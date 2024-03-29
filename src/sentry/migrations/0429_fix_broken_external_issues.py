# Generated by Django 2.2.28 on 2023-04-25 23:51

from django.db import migrations

from sentry.new_migrations.migrations import CheckedMigration


def fix_broken_external_issues(apps, schema_editor):
    ExternalIssue = apps.get_model("sentry", "ExternalIssue")

    # Broken issues from redash: https://redash.getsentry.net/queries/4012/source
    broken_ids = [636683, 636687, 636692]
    old_organization_id = 443715
    new_organization_id = 5417824
    for issue in ExternalIssue.objects.filter(
        id__in=broken_ids, organization_id=old_organization_id
    ):
        issue.organization_id = new_organization_id
        issue.save()


class Migration(CheckedMigration):
    # This flag is used to mark that a migration shouldn't be automatically run in production. For
    # the most part, this should only be used for operations where it's safe to run the migration
    # after your code has deployed. So this should not be used for most operations that alter the
    # schema of a table.
    # Here are some things that make sense to mark as dangerous:
    # - Large data migrations. Typically we want these to be run manually by ops so that they can
    #   be monitored and not block the deploy for a long period of time while they run.
    # - Adding indexes to large tables. Since this can take a long time, we'd generally prefer to
    #   have ops run this and not block the deploy. Note that while adding an index is a schema
    #   change, it's completely safe to run the operation after the code has deployed.
    is_dangerous = False

    dependencies = [
        ("sentry", "0428_backfill_denormalize_notification_actor"),
    ]

    operations = [
        migrations.RunPython(
            fix_broken_external_issues,
            reverse_code=migrations.RunPython.noop,
            hints={"tables": ["sentry_externalissue"]},
        ),
    ]
