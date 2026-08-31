from django.db import migrations, models
import django.db.models.deletion
import uuid
from decimal import Decimal


def backfill_contacts_and_legacy(apps, schema_editor):
    BusinessInfo = apps.get_model("quickstart", "BusinessInfo")
    BusinessInfo.objects.filter(isActive=True).exclude(
        widget_subscription__status__in=["active", "trialing"]
    ).update(legacy_grandfathered=True)

    Booking = apps.get_model("quickstart", "Booking")
    Contact = apps.get_model("quickstart", "Contact")
    from quickstart.services.crm_stats import backfill_contacts_and_stats

    backfill_contacts_and_stats(Booking, Contact)


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0251_businessinfo_onboarding_survey"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessinfo",
            name="commission_waived_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="payout_interval",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("daily", "Daily"),
                    ("weekly", "Weekly"),
                    ("monthly", "Monthly"),
                ],
                default="daily",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="payout_weekly_anchor",
            field=models.CharField(blank=True, default="monday", max_length=9),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="payout_monthly_anchor",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="instant_payouts_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="businessinfo",
            name="legacy_grandfathered",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="contact",
            name="lifetime_value",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="contact",
            name="booking_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="contact",
            name="first_booking_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contact",
            name="last_booking_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contact",
            name="last_activity_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="booking",
            name="payout_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processed", "Processed"),
                    ("failed", "Failed"),
                    ("not_applicable", "Not Applicable"),
                    ("settled", "Settled"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="payout",
            name="stripe_transfer_id",
            field=models.CharField(
                blank=True, db_index=True, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="payout",
            name="stripe_payout_id",
            field=models.CharField(
                blank=True, db_index=True, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="payout",
            name="method",
            field=models.CharField(
                choices=[("standard", "Standard"), ("instant", "Instant")],
                default="standard",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="payout",
            name="failure_code",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="payout",
            name="failure_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="payout",
            name="fee_amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=10
            ),
        ),
        migrations.CreateModel(
            name="ClientSegment",
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
                ("name", models.CharField(max_length=120)),
                ("definition", models.JSONField(blank=True, default=dict)),
                ("is_builtin", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="client_segments",
                        to="quickstart.businessinfo",
                    ),
                ),
            ],
            options={
                "db_table": "crm_client_segments",
                "ordering": ["name"],
                "unique_together": {("business", "name")},
            },
        ),
        migrations.RunPython(
            backfill_contacts_and_legacy,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
