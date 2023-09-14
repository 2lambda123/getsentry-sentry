from typing import List, Literal, Optional

import sentry_sdk
from django.utils.encoding import force_bytes, force_str
from drf_spectacular.utils import extend_schema
from packaging.version import Version
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response
from typing_extensions import TypedDict

from sentry import eventstore, features
from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import region_silo_endpoint
from sentry.api.bases.project import ProjectEndpoint
from sentry.apidocs.constants import RESPONSE_FORBIDDEN, RESPONSE_NOT_FOUND, RESPONSE_UNAUTHORIZED
from sentry.apidocs.parameters import EventParams, GlobalParams
from sentry.apidocs.utils import inline_sentry_response_serializer
from sentry.models.artifactbundle import (
    ArtifactBundle,
    ArtifactBundleArchive,
    DebugIdArtifactBundle,
    ReleaseArtifactBundle,
    SourceFileType,
)
from sentry.models.project import Project
from sentry.models.release import Release
from sentry.models.releasefile import ReleaseFile
from sentry.sdk_updates import get_sdk_index
from sentry.utils.javascript import find_sourcemap
from sentry.utils.safe import get_path
from sentry.utils.urls import non_standard_url_join

MIN_JS_SDK_VERSION_FOR_DEBUG_IDS = "7.56.0"

NO_DEBUG_ID_SDKS = {
    "sentry.javascript.capacitor",
    "sentry.javascript.react-native",
    "sentry.javascript.wasm",
    "sentry.javascript.cordova",
    "sentry.javascript.nextjs",
    "sentry.javascript.sveltekit",
}


class SourceMapDebugIdProcessResult(TypedDict):
    debug_id: Optional[str]
    uploaded_source_file_with_correct_debug_id: bool
    uploaded_source_map_with_correct_debug_id: bool


class SourceMapReleaseProcessResult(TypedDict):
    matching_source_file_names: List[str]
    matching_source_map_name: Optional[str]
    source_map_reference: Optional[str]
    source_file_lookup_result: Literal["found", "wrong-dist", "unsuccessful"]
    source_map_lookup_result: Literal["found", "wrong-dist", "unsuccessful"]


class SourceMapDebugFrame(TypedDict):
    debug_id_process: SourceMapDebugIdProcessResult
    release_process: Optional[SourceMapReleaseProcessResult]


class SourceMapDebugException(TypedDict):
    frames: List[SourceMapDebugFrame]


class SourceMapDebugResponse(TypedDict):
    dist: Optional[str]
    release: Optional[str]
    exceptions: List[SourceMapDebugException]
    has_debug_ids: bool
    sdk_version: Optional[str]
    project_has_some_artifact_bundle: bool
    release_has_some_artifact: bool
    has_uploaded_some_artifact_with_a_debug_id: bool
    sdk_debug_id_support: Literal["not-supported", "unofficial-sdk", "needs-upgrade", "full"]


@region_silo_endpoint
@extend_schema(tags=["Events"])
class SourceMapDebugBlueThunderEditionEndpoint(ProjectEndpoint):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    owner = ApiOwner.WEB_FRONTEND_SDKS

    @extend_schema(
        operation_id="Get Debug Information Related to Source Maps for a Given Event",
        parameters=[
            GlobalParams.ORG_SLUG,
            GlobalParams.PROJECT_SLUG,
            EventParams.EVENT_ID,
        ],
        request=None,
        responses={
            200: inline_sentry_response_serializer("SourceMapDebug", SourceMapDebugResponse),
            401: RESPONSE_UNAUTHORIZED,
            403: RESPONSE_FORBIDDEN,
            404: RESPONSE_NOT_FOUND,
        },
    )
    def get(self, request: Request, project: Project, event_id: str) -> Response:
        """
        Return a list of source map errors for a given event.
        """

        if not features.has(
            "organizations:source-maps-debugger-blue-thunder-edition",
            project.organization,
            actor=request.user,
        ):
            raise NotFound(
                detail="Endpoint not available without 'organizations:source-maps-debugger-blue-thunder-edition' feature flag"
            )

        event = eventstore.backend.get_event_by_id(project.id, event_id)
        if event is None:
            raise NotFound(detail="Event not found")

        event_data = event.data

        release = None
        if event.release is not None:
            try:
                release = Release.objects.get(
                    organization=project.organization, version=event.release
                )
            except Release.DoesNotExist:
                pass

        # get general information about what has been uploaded
        project_has_some_artifact_bundle = ArtifactBundle.objects.filter(
            projectartifactbundle__project_id=project.id,
        ).exists()
        has_uploaded_release_bundle_with_release = False
        has_uploaded_artifact_bundle_with_release = False
        if release is not None:
            has_uploaded_release_bundle_with_release = ReleaseFile.objects.filter(
                release_id=release.id
            ).exists()
            has_uploaded_artifact_bundle_with_release = ReleaseArtifactBundle.objects.filter(
                organization_id=project.organization_id, release_name=release.version
            ).exists()
        has_uploaded_some_artifact_with_a_debug_id = DebugIdArtifactBundle.objects.filter(
            organization_id=project.organization_id,
            artifact_bundle__projectartifactbundle__project_id=project.id,
        ).exists()

        debug_images = get_path(event_data, "debug_meta", "images")
        debug_images = debug_images if debug_images is not None else []

        # get information about which debug ids on the event have uploaded artifacts
        debug_ids = [
            debug_image["debug_id"]
            for debug_image in debug_images
            if debug_image["type"] == "sourcemap"
        ][0:100]
        debug_id_artifact_bundles = DebugIdArtifactBundle.objects.filter(
            artifact_bundle__projectartifactbundle__project_id=project.id,
            debug_id__in=debug_ids,
        )
        debug_ids_with_uploaded_source_file = set()
        debug_ids_with_uploaded_source_map = set()
        for debug_id_artifact_bundle in debug_id_artifact_bundles:
            if (
                SourceFileType(debug_id_artifact_bundle.source_file_type) == SourceFileType.SOURCE
                or SourceFileType(debug_id_artifact_bundle.source_file_type)
                == SourceFileType.MINIFIED_SOURCE
            ):
                debug_ids_with_uploaded_source_file.add(str(debug_id_artifact_bundle.debug_id))
            elif (
                SourceFileType(debug_id_artifact_bundle.source_file_type)
                == SourceFileType.SOURCE_MAP
            ):
                debug_ids_with_uploaded_source_map.add(str(debug_id_artifact_bundle.debug_id))

        # Get all abs paths and query for their existence so that we can match release artifacts
        release_process_abs_path_data = {}
        if release is not None:
            abs_paths = get_abs_paths_in_event(event_data)
            for abs_path in abs_paths:
                path_data = get_source_file_data(abs_path, project, release, event)
                release_process_abs_path_data[abs_path] = path_data

        # build information about individual exceptions and their stack traces
        processed_exceptions = []
        exception_values = get_path(event_data, "exception", "values")
        if exception_values is not None:
            for exception_value in exception_values:
                processed_frames = []
                frames = get_path(exception_value, "raw_stacktrace", "frames")
                frames = frames or get_path(exception_value, "stacktrace", "frames")
                if frames is not None:
                    for frame in frames:
                        abs_path = get_path(frame, "abs_path")
                        debug_id = next(
                            (
                                debug_image["debug_id"]
                                for debug_image in debug_images
                                if debug_image["type"] == "sourcemap"
                                and abs_path == debug_image["code_file"]
                            ),
                            None,
                        )
                        processed_frames.append(
                            {
                                "debug_id_process": {
                                    "debug_id": debug_id,
                                    "uploaded_source_file_with_correct_debug_id": debug_id
                                    in debug_ids_with_uploaded_source_file,
                                    "uploaded_source_map_with_correct_debug_id": debug_id
                                    in debug_ids_with_uploaded_source_map,
                                },
                                "release_process": release_process_abs_path_data.get(abs_path),
                            }
                        )
                processed_exceptions.append({"frames": processed_frames})

        return Response(
            {
                "dist": event.dist,
                "release": event.release,
                "exceptions": processed_exceptions,
                "has_debug_ids": event_has_debug_ids(event_data),
                "sdk_version": get_path(event_data, "sdk", "version"),
                "project_has_some_artifact_bundle": project_has_some_artifact_bundle,
                "release_has_some_artifact": has_uploaded_release_bundle_with_release
                or has_uploaded_artifact_bundle_with_release,
                "has_uploaded_some_artifact_with_a_debug_id": has_uploaded_some_artifact_with_a_debug_id,
                "sdk_debug_id_support": get_sdk_debug_id_support(event_data),
            }
        )


def get_source_file_data(abs_path, project, release, event):
    filenme_choices = ReleaseFile.normalize(abs_path)

    path_data = {
        "matching_source_file_names": filenme_choices,
        "matching_source_map_name": None,
        "source_map_reference": None,
        "source_file_lookup_result": "unsuccessful",
        "source_map_lookup_result": "unsuccessful",
    }

    possible_release_files = (
        ReleaseFile.objects.filter(
            organization_id=project.organization_id,
            release_id=release.id,
            name__in=filenme_choices,
        )
        .exclude(artifact_count=0)
        .select_related("file")
    )
    if len(possible_release_files) > 0:
        path_data["source_file_lookup_result"] = "wrong-dist"
    for possible_release_file in possible_release_files:
        if possible_release_file.ident == ReleaseFile.get_ident(
            possible_release_file.name, event.dist
        ):
            path_data["source_file_lookup_result"] = "found"
            source_map_reference = None
            sourcemap_header = None
            if possible_release_file.file.headers:
                headers = ArtifactBundleArchive.normalize_headers(
                    possible_release_file.file.headers
                )
                sourcemap_header = headers.get("sourcemap", headers.get("x-sourcemap"))
                sourcemap_header = (
                    force_bytes(sourcemap_header) if sourcemap_header is not None else None
                )

            try:
                source_map_reference = find_sourcemap(
                    sourcemap_header, possible_release_file.file.getfile().read()
                )
                if source_map_reference is not None:
                    source_map_reference = force_str(source_map_reference)
            except AssertionError:
                pass

            matching_source_map_name = None
            if source_map_reference is not None:
                if source_map_reference.startswith("data:"):
                    source_map_reference = "Inline Sourcemap"
                    path_data["source_map_lookup_result"] = "found"
                else:
                    matching_source_map_name = get_matching_source_map_location(
                        possible_release_file.name, source_map_reference
                    )

            if matching_source_map_name is not None:
                path_data["source_map_lookup_result"] = get_release_files_status_by_url(
                    event, project, release, [matching_source_map_name]
                )

            return {
                "matching_source_file_names": filenme_choices,
                "matching_source_map_name": matching_source_map_name,
                "source_map_reference": source_map_reference,
                "source_file_lookup_result": "found",
                "source_map_lookup_result": path_data["source_map_lookup_result"],
            }

    possible_release_artifact_bundles = ReleaseArtifactBundle.objects.filter(
        organization_id=project.organization.id,
        release_name=release.version,
        artifact_bundle__projectartifactbundle__project_id=project.id,
        artifact_bundle__artifactbundleindex__organization_id=project.organization.id,
        artifact_bundle__artifactbundleindex__url__in=filenme_choices,
    )
    if len(possible_release_artifact_bundles) > 0:
        path_data["source_file_lookup_result"] = (
            "wrong-dist"
            if path_data["source_file_lookup_result"] == "unsuccessful"
            else path_data["source_file_lookup_result"]
        )
    for possible_release_artifact_bundle in possible_release_artifact_bundles:
        if possible_release_artifact_bundle.dist_name == (event.dist or ""):
            found_source_file_path = None
            source_map_reference = None
            with ArtifactBundleArchive(
                possible_release_artifact_bundle.artifact_bundle.file.getfile()
            ) as archive:
                matching_file = None
                sourcemap_header = None
                for filename_choice in filenme_choices:
                    try:
                        matching_file, headers = archive.get_file_by_url(filename_choice)
                        sourcemap_header = headers.get("sourcemap", headers.get("x-sourcemap"))
                        sourcemap_header = (
                            force_bytes(sourcemap_header) if sourcemap_header is not None else None
                        )
                        found_source_file_path = filename_choice
                        break
                    except Exception:
                        continue
                if matching_file is not None:
                    try:
                        source_map_reference = find_sourcemap(
                            sourcemap_header, matching_file.read()
                        )
                    except AssertionError:
                        pass
                    source_map_reference = (
                        force_str(source_map_reference)
                        if source_map_reference is not None
                        else None
                    )

            matching_source_map_name = None
            if source_map_reference is not None:
                if source_map_reference.startswith("data:"):
                    source_map_reference = "Inline Sourcemap"
                    path_data["source_map_lookup_result"] = "found"
                elif found_source_file_path is not None:
                    matching_source_map_name = get_matching_source_map_location(
                        found_source_file_path, source_map_reference
                    )

            if matching_source_map_name is not None:
                path_data["source_map_lookup_result"] = get_artifact_bundle_file_status_by_url(
                    event, project, release, [matching_source_map_name]
                )

            return {
                "matching_source_file_names": filenme_choices,
                "matching_source_map_name": matching_source_map_name,
                "source_file_lookup_result": "found",
                "source_map_reference": source_map_reference,
                "source_map_lookup_result": path_data["source_map_lookup_result"],
            }

    return path_data


def get_release_files_status_by_url(event, project, release, possible_urls):
    result = "unsuccessful"
    possible_release_files = ReleaseFile.objects.filter(
        organization_id=project.organization_id,
        release_id=release.id,
        name__in=possible_urls,
    ).exclude(artifact_count=0)
    if len(possible_release_files) > 0:
        result = "wrong-dist"
    for possible_release_file in possible_release_files:
        if possible_release_file.ident == ReleaseFile.get_ident(
            possible_release_file.name, event.dist
        ):
            return "found"
    return result


def get_artifact_bundle_file_status_by_url(event, project, release, possible_urls):
    result = "unsuccessful"
    possible_release_artifact_bundles = ReleaseArtifactBundle.objects.filter(
        organization_id=project.organization.id,
        release_name=release.version,
        artifact_bundle__projectartifactbundle__project_id=project.id,
        artifact_bundle__artifactbundleindex__organization_id=project.organization.id,
        artifact_bundle__artifactbundleindex__url__in=possible_urls,
    )
    if len(possible_release_artifact_bundles) > 0:
        result = "wrong-dist"
    for possible_release_artifact_bundle in possible_release_artifact_bundles:
        if possible_release_artifact_bundle.dist_name == (event.dist or ""):
            return "found"
    return result


def get_matching_source_map_location(source_file_path, source_map_reference):
    return non_standard_url_join(force_str(source_file_path), force_str(source_map_reference))


def event_has_debug_ids(event_data):
    debug_images = get_path(event_data, "debug_meta", "images")
    if debug_images is None:
        return False
    else:
        for debug_image in debug_images:
            if debug_image["type"] == "sourcemap":
                return True
        return False


def get_sdk_debug_id_support(event_data):
    sdk_name = get_path(event_data, "sdk", "name")

    official_sdks = None
    try:
        sdk_release_registry = get_sdk_index()
        official_sdks = [
            sdk for sdk in sdk_release_registry.keys() if sdk.startswith("sentry.javascript.")
        ]
    except Exception as e:
        sentry_sdk.capture_exception(e)
        pass

    if official_sdks is None or len(official_sdks) == 0:
        # Fallback list if release registry is not available
        official_sdks = [
            "sentry.javascript.angular",
            "sentry.javascript.angular-ivy",
            "sentry.javascript.browser",
            "sentry.javascript.capacitor",
            "sentry.javascript.cordova",
            "sentry.javascript.electron",
            "sentry.javascript.gatsby",
            "sentry.javascript.nextjs",
            "sentry.javascript.node",
            "sentry.javascript.opentelemetry-node",
            "sentry.javascript.react",
            "sentry.javascript.react-native",
            "sentry.javascript.remix",
            "sentry.javascript.svelte",
            "sentry.javascript.sveltekit",
            "sentry.javascript.vue",
        ]

    if sdk_name not in official_sdks or sdk_name is None:
        return "unofficial-sdk"
    elif sdk_name in NO_DEBUG_ID_SDKS:
        return "not-supported"

    sdk_version = get_path(event_data, "sdk", "version")
    if sdk_version is None:
        return "unofficial-sdk"

    return (
        "full"
        if Version(sdk_version) >= Version(MIN_JS_SDK_VERSION_FOR_DEBUG_IDS)
        else "needs-upgrade"
    )


def get_abs_paths_in_event(event_data):
    abs_paths = set()
    exception_values = get_path(event_data, "exception", "values")
    if exception_values is not None:
        for exception_value in exception_values:
            stacktrace = get_path(exception_value, "raw_stacktrace") or get_path(
                exception_value, "stacktrace"
            )
            frames = get_path(stacktrace, "frames")
            if frames is not None:
                for frame in frames:
                    abs_path = get_path(frame, "abs_path")
                    if abs_path:
                        abs_paths.add(abs_path)
    return abs_paths