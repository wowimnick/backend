# Optional badge label on membership products; sync ORM with existing NOT NULL column if present.

from django.db import migrations, models


def _ensure_badge_text(apps, schema_editor):
    MembershipProduct = apps.get_model("quickstart", "MembershipProduct")
    table = MembershipProduct._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        desc = schema_editor.connection.introspection.get_table_description(cursor, table)
    columns = {row.name for row in desc}
    if "badge_text" not in columns:
        field = MembershipProduct._meta.get_field("badge_text")
        schema_editor.add_field(MembershipProduct, field)
        return
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE membership_products SET badge_text = '' WHERE badge_text IS NULL"
            )
            cursor.execute(
                "ALTER TABLE membership_products ALTER COLUMN badge_text SET DEFAULT ''"
            )


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0211_membershipproduct_widget_button_config"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="membershipproduct",
                    name="badge_text",
                    field=models.CharField(
                        blank=True,
                        default="",
                        help_text="Optional short label on pricing cards (e.g. Popular).",
                        max_length=100,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(_ensure_badge_text, _noop_reverse),
            ],
        ),
    ]
