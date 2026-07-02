import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0247_featured_homepage_review_display_comment"),
    ]

    operations = [
        migrations.CreateModel(
            name="IgnoredScheduleWarning",
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
                ("ignored_at", models.DateTimeField(auto_now_add=True)),
                ("note", models.TextField(blank=True, default="")),
                (
                    "admin_user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ignored_schedule_warnings",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "class_id",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ignored_schedule_warnings",
                        to="quickstart.classesmain",
                    ),
                ),
            ],
            options={
                "db_table": "ignored_schedule_warnings",
            },
        ),
        migrations.AddConstraint(
            model_name="ignoredschedulewarning",
            constraint=models.UniqueConstraint(
                fields=("admin_user", "class_id"),
                name="unique_admin_class_schedule_warning_ignore",
            ),
        ),
    ]
