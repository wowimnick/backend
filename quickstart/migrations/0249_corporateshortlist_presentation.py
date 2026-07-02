# Safety net: ensure presentation column exists after linear 0248 chain.
# Depends on 0240_corporateshortlist_presentation so Django applies that branch
# migration before this noop/idempotent check on environments still at 0248.

from django.db import migrations


def ensure_presentation_column(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        ALTER TABLE corporate_shortlists
        ADD COLUMN IF NOT EXISTS presentation jsonb NOT NULL
        DEFAULT '{}'::jsonb;
        """
    )


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0248_ignored_schedule_warning"),
        ("quickstart", "0240_corporateshortlist_presentation"),
    ]

    operations = [
        migrations.RunPython(ensure_presentation_column, migrations.RunPython.noop),
    ]
