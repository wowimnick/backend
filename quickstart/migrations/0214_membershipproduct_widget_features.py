# Widget feature flags JSON on membership products; sync ORM with NOT NULL column if present.

from django.db import migrations, models


def _ensure_widget_features(apps, schema_editor):
    MembershipProduct = apps.get_model("quickstart", "MembershipProduct")
    table = MembershipProduct._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        desc = schema_editor.connection.introspection.get_table_description(cursor, table)
    columns = {row.name for row in desc}
    if "widget_features" not in columns:
        field = models.JSONField(
            blank=True,
            default=dict,
            help_text="Optional feature flags / config for widget membership UI (e.g. display options).",
        )
        field.set_attributes_from_name("widget_features")
        schema_editor.add_field(MembershipProduct, field)
        return
    # from_state.apps in RunPython does not include this field yet; use raw SQL.
    conn = schema_editor.connection
    with conn.cursor() as cursor:
        if conn.vendor == "postgresql":
            cursor.execute(
                "UPDATE membership_products SET widget_features = '{}'::jsonb "
                "WHERE widget_features IS NULL"
            )
        elif conn.vendor == "sqlite":
            cursor.execute(
                "UPDATE membership_products SET widget_features = '{}' "
                "WHERE widget_features IS NULL"
            )
        else:
            cursor.execute(
                "UPDATE membership_products SET widget_features = %s "
                "WHERE widget_features IS NULL",
                ["{}"],
            )


def _noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0213_alter_customuser_options_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="membershipproduct",
                    name="widget_features",
                    field=models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Optional feature flags / config for widget membership UI (e.g. display options).",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunPython(_ensure_widget_features, _noop_reverse),
            ],
        ),
    ]
