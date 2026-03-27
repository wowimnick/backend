# Optional widget CTA label; sync ORM with NOT NULL column on membership_products if present.

from django.db import migrations, models


def _ensure_widget_cta_label(apps, schema_editor):
    MembershipProduct = apps.get_model("quickstart", "MembershipProduct")
    table = MembershipProduct._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        desc = schema_editor.connection.introspection.get_table_description(cursor, table)
    columns = {row.name for row in desc}
    if "widget_cta_label" not in columns:
        field = MembershipProduct._meta.get_field("widget_cta_label")
        schema_editor.add_field(MembershipProduct, field)
        return
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE membership_products SET widget_cta_label = '' "
                "WHERE widget_cta_label IS NULL"
            )
            cursor.execute(
                "ALTER TABLE membership_products ALTER COLUMN widget_cta_label SET DEFAULT ''"
            )


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0214_membershipproduct_widget_features"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="membershipproduct",
                    name="widget_cta_label",
                    field=models.CharField(
                        blank=True,
                        default="",
                        help_text="Optional CTA label override for the membership widget (e.g. Subscribe).",
                        max_length=200,
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(_ensure_widget_cta_label, _noop_reverse),
            ],
        ),
    ]
