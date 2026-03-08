# Generated for FirstPurchaseGiftCardSent (first-purchase gift card tracking)

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0205_businessinfo_last_payout_connect_reminder_sent"),
    ]

    operations = [
        migrations.CreateModel(
            name="FirstPurchaseGiftCardSent",
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
                    "customer_email",
                    models.CharField(
                        db_index=True,
                        help_text="Normalized (e.g. lowercase) email; one gift card per customer.",
                        max_length=255,
                        unique=True,
                    ),
                ),
                (
                    "order_total",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Grand total (after tax) of the first order that triggered this.",
                        max_digits=10,
                    ),
                ),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                (
                    "payment_intent_id",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "gift_card",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="first_purchase_sent_record",
                        to="quickstart.giftcard",
                    ),
                ),
            ],
            options={
                "db_table": "first_purchase_gift_card_sent",
                "ordering": ["-sent_at"],
            },
        ),
    ]
