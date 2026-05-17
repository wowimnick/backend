from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0242_businessinfo_instagram_followers"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="google_maps_url",
            field=models.URLField(
                blank=True,
                help_text="Google Maps URL for Apify review scraper input.",
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="google_reviews_last_scraped_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Number of review items returned by Apify in the most recent run.",
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="google_reviews_sync_status",
            field=models.CharField(
                choices=[
                    ("pending", "pending"),
                    ("ok", "ok"),
                    ("not_found", "not_found"),
                    ("error", "error"),
                    ("running", "running"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="google_reviews_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
