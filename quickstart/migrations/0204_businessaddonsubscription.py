# Business addon subscriptions (e.g. marketplace email branding)

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0203_businessinfo_marketplace_email_branding"),
    ]

    operations = [
        migrations.CreateModel(
            name="BusinessAddonSubscription",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("addon_type", models.CharField(db_index=True, help_text="e.g. marketplace_email_branding", max_length=64)),
                ("stripe_subscription_id", models.CharField(blank=True, db_index=True, max_length=255, null=True, unique=True)),
                ("stripe_customer_id", models.CharField(blank=True, max_length=255, null=True)),
                ("stripe_price_id", models.CharField(blank=True, max_length=255, null=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("trialing", "Trialing"), ("past_due", "Past Due"), ("canceled", "Canceled"), ("incomplete", "Incomplete"), ("incomplete_expired", "Incomplete Expired")], db_index=True, default="incomplete", max_length=30)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("cancel_at_period_end", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="addon_subscriptions", to="quickstart.businessinfo")),
            ],
            options={
                "db_table": "business_addon_subscriptions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="businessaddonsubscription",
            index=models.Index(fields=["business", "addon_type", "status"], name="business_ad_busines_8b0b0d_idx"),
        ),
    ]
