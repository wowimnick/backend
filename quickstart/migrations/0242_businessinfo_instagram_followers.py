from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0241_payment_view_platform_revenue_permission"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="instagram_follower_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="instagram_followers_synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="instagram_sync_status",
            field=models.CharField(
                choices=[
                    ("pending", "pending"),
                    ("ok", "ok"),
                    ("not_found", "not_found"),
                    ("private", "private"),
                    ("error", "error"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
