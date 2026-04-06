# Generated manually for SearchLog

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quickstart", "0221_businessinfo_require_participant_names"),
    ]

    operations = [
        migrations.CreateModel(
            name="SearchLog",
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
                ("query", models.CharField(blank=True, max_length=255)),
                ("location", models.CharField(blank=True, max_length=255)),
                ("province", models.CharField(blank=True, max_length=50)),
                ("latitude", models.FloatField(blank=True, null=True)),
                ("longitude", models.FloatField(blank=True, null=True)),
                ("session_id", models.CharField(blank=True, max_length=100)),
                ("results_count", models.IntegerField(default=0)),
                ("timestamp", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="search_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "search_logs",
                "ordering": ["-timestamp"],
            },
        ),
        migrations.AddIndex(
            model_name="searchlog",
            index=models.Index(
                fields=["location", "province"], name="search_logs_loc_prov_idx"
            ),
        ),
    ]
