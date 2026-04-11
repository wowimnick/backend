# Generated manually for multi-location support

import django.contrib.gis.db.models.fields
import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0223_alter_notification_notification_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessLocation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                ("address", models.CharField(max_length=255)),
                (
                    "unit",
                    models.CharField(blank=True, max_length=50, null=True),
                ),
                ("city", models.CharField(max_length=100)),
                ("state", models.CharField(max_length=100)),
                ("zip_code", models.CharField(blank=True, default="", max_length=20)),
                (
                    "latitude",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=10, null=True
                    ),
                ),
                (
                    "longitude",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=11, null=True
                    ),
                ),
                (
                    "point",
                    django.contrib.gis.db.models.fields.PointField(
                        blank=True,
                        help_text="Geographic location for spatial queries.",
                        null=True,
                        srid=4326,
                    ),
                ),
                ("show_exact_location", models.BooleanField(default=True)),
                ("is_primary", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locations",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "business_location",
                "ordering": ["-is_primary", "name"],
            },
        ),
        migrations.AddField(
            model_name="classesmain",
            name="location_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="classes",
                to="quickstart.businesslocation",
            ),
        ),
    ]
