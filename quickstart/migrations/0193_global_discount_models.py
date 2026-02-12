# Generated manually for GlobalDiscount and AppliedGlobalDiscount

import uuid
from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0192_giftcard_send_to_self"),
    ]

    operations = [
        migrations.CreateModel(
            name="GlobalDiscount",
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
                    "name",
                    models.CharField(
                        help_text="Internal name for this global discount (e.g. 'Summer Sale 2025').",
                        max_length=150,
                    ),
                ),
                (
                    "discount_type",
                    models.CharField(
                        choices=[
                            ("percentage", "Percentage"),
                            ("fixed_amount", "Fixed Amount"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "value",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="The value of the discount (e.g., 20.00 for 20% or 10.00 for $10).",
                        max_digits=10,
                    ),
                ),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "valid_from",
                    models.DateTimeField(
                        blank=True,
                        help_text="Leave blank for no start date.",
                        null=True,
                    ),
                ),
                (
                    "valid_to",
                    models.DateTimeField(
                        blank=True,
                        help_text="Leave blank for no expiration date.",
                        null=True,
                    ),
                ),
                (
                    "usage_limit",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Max number of times this can be used in total.",
                        null=True,
                    ),
                ),
                (
                    "usage_count",
                    models.PositiveIntegerField(default=0, editable=False),
                ),
                (
                    "min_purchase_amount",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text="The minimum booking subtotal required to use this discount.",
                        max_digits=10,
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "global_discounts",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AppliedGlobalDiscount",
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
                (
                    "amount_saved",
                    models.DecimalField(decimal_places=2, max_digits=10),
                ),
                ("applied_at", models.DateTimeField(auto_now_add=True)),
                (
                    "booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="applied_global_discounts",
                        to="quickstart.booking",
                    ),
                ),
                (
                    "global_discount",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="applied_to_bookings",
                        to="quickstart.globaldiscount",
                    ),
                ),
            ],
            options={
                "db_table": "applied_global_discounts",
                "unique_together": {("booking", "global_discount")},
            },
        ),
    ]
