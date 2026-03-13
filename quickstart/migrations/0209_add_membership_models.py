# Customer subscription / membership models

import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0208_widgetsubscription_one_per_business"),
    ]

    operations = [
        migrations.CreateModel(
            name="MembershipProduct",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("price", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("currency", models.CharField(default="CAD", max_length=3)),
                ("billing_interval", models.CharField(choices=[("month", "Month"), ("year", "Year")], default="month", max_length=10)),
                ("access_type", models.CharField(choices=[("unlimited", "Unlimited"), ("credits", "Credits")], default="unlimited", max_length=20)),
                ("credit_allowance", models.PositiveIntegerField(blank=True, help_text="e.g. 15; only used when access_type=credits", null=True)),
                ("credit_unit", models.CharField(blank=True, help_text="e.g. '2-hour sessions'", max_length=100)),
                ("is_active", models.BooleanField(default=True)),
                ("requires_approval", models.BooleanField(default=False, help_text="If True, show application form link")),
                ("stripe_price_id", models.CharField(blank=True, max_length=255, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="membership_products", to="quickstart.businessinfo")),
            ],
            options={
                "db_table": "membership_products",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="CustomerMembership",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("stripe_subscription_id", models.CharField(blank=True, db_index=True, max_length=255, null=True, unique=True)),
                ("stripe_customer_id", models.CharField(blank=True, max_length=255, null=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("trialing", "Trialing"), ("past_due", "Past Due"), ("canceled", "Canceled"), ("paused", "Paused"), ("pending_approval", "Pending Approval"), ("incomplete", "Incomplete")], db_index=True, default="active", max_length=30)),
                ("current_period_start", models.DateTimeField(blank=True, null=True)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("cancel_at_period_end", models.BooleanField(default=False)),
                ("source", models.CharField(help_text="e.g. widget, manual, import", max_length=50)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("contact", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="quickstart.contact")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="quickstart.membershipproduct")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="customer_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "customer_memberships",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="membershipproduct",
            name="applicable_classes",
            field=models.ManyToManyField(blank=True, help_text="Empty = all classes", related_name="membership_products", to="quickstart.classesmain"),
        ),
        migrations.AddIndex(
            model_name="customermembership",
            index=models.Index(fields=["product", "status"], name="customer_me_product_7a8c9d_idx"),
        ),
        migrations.CreateModel(
            name="MembershipCreditLedger",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("period_start", models.DateField()),
                ("credits_used", models.PositiveIntegerField(default=1)),
                ("action", models.CharField(choices=[("consumed", "Consumed"), ("refunded", "Refunded"), ("reset", "Reset"), ("manual", "Manual")], max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("booking", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="membership_credit_entries", to="quickstart.booking")),
                ("membership", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ledger_entries", to="quickstart.customermembership")),
            ],
            options={
                "db_table": "membership_credit_ledger",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="MembershipPayment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("stripe_invoice_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("stripe_payment_intent_id", models.CharField(blank=True, max_length=255, null=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("platform_fee_amount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10)),
                ("net_payout_amount", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(0)])),
                ("status", models.CharField(choices=[("paid", "Paid"), ("failed", "Failed"), ("refunded", "Refunded")], default="paid", max_length=30)),
                ("period_start", models.DateTimeField(blank=True, null=True)),
                ("period_end", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("membership", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="quickstart.customermembership")),
            ],
            options={
                "db_table": "membership_payments",
                "ordering": ["-created_at"],
            },
        ),
    ]
