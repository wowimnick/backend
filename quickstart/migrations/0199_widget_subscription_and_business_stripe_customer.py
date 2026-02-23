# Widget subscription ($50/mo) and BusinessInfo.stripe_customer_id for platform billing

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0198_remove_classesmain_category_subcategory"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="stripe_customer_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Stripe Customer ID for platform billing (e.g. widget subscription).",
                max_length=255,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="WidgetSubscription",
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
                (
                    "stripe_subscription_id",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=255,
                        null=True,
                        unique=True,
                    ),
                ),
                (
                    "stripe_customer_id",
                    models.CharField(
                        blank=True, db_index=True, max_length=255, null=True
                    ),
                ),
                (
                    "stripe_price_id",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("trialing", "Trialing"),
                            ("past_due", "Past Due"),
                            ("canceled", "Canceled"),
                            ("incomplete", "Incomplete"),
                            ("incomplete_expired", "Incomplete Expired"),
                        ],
                        db_index=True,
                        default="incomplete",
                        max_length=30,
                    ),
                ),
                (
                    "current_period_end",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="widget_subscriptions",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "widget_subscriptions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="widgetsubscription",
            index=models.Index(
                fields=["business", "status"],
                name="widget_subs_business_status_idx",
            ),
        ),
    ]
