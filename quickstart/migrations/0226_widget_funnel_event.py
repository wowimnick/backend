# Widget booking funnel analytics (anonymous events)

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0225_migrate_business_locations_data"),
    ]

    operations = [
        migrations.CreateModel(
            name="WidgetFunnelEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("session_id", models.CharField(db_index=True, max_length=64)),
                ("event", models.CharField(db_index=True, max_length=50)),
                ("step", models.CharField(blank=True, max_length=30)),
                ("device_type", models.CharField(blank=True, max_length=10)),
                ("class_id", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="widget_funnel_events",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "widget_funnel_events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="widgetfunnelevent",
            index=models.Index(
                fields=["business", "created_at"],
                name="wfe_biz_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="widgetfunnelevent",
            index=models.Index(
                fields=["business", "event", "created_at"],
                name="wfe_biz_event_created_idx",
            ),
        ),
    ]
