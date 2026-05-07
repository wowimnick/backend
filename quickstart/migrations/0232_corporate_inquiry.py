# Generated manually for corporate B2B leads

from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0231_classcollection_search_and_placement_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="CorporateInquiry",
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
                ("company_name", models.CharField(max_length=200)),
                ("contact_name", models.CharField(max_length=150)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("phone", models.CharField(blank=True, default="", max_length=50)),
                (
                    "company_size",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("", "Prefer not to say"),
                            ("1-10", "1–10"),
                            ("11-50", "11–50"),
                            ("51-200", "51–200"),
                            ("201-500", "201–500"),
                            ("501+", "501+"),
                        ],
                        default="",
                        max_length=20,
                    ),
                ),
                ("message", models.TextField(blank=True, default="", max_length=5000)),
                ("meta", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "db_table": "corporate_inquiries",
                "ordering": ["-created_at"],
            },
        ),
    ]
