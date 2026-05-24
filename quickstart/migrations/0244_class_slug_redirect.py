import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0243_businessinfo_google_maps_url"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClassSlugRedirect",
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
                ("slug", models.SlugField(db_index=True, max_length=255, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "class_ref",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slug_redirects",
                        to="quickstart.classesmain",
                    ),
                ),
            ],
            options={
                "db_table": "class_slug_redirects",
            },
        ),
    ]
