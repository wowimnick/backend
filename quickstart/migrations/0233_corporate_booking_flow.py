# Corporate shortlists, options, bookings, events + CorporateInquiry permissions

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _default_corp_ref():
    return f"CE-CORP-{uuid.uuid4().hex[:6].upper()}"


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("quickstart", "0232_corporate_inquiry"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="corporateinquiry",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("access_corporate_admin", "Can access corporate inquiry admin"),
                    ("manage_corporate_shortlists", "Can manage corporate shortlists"),
                    ("manage_corporate_bookings", "Can manage corporate bookings and billing"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CorporateShortlist",
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
                    "token",
                    models.UUIDField(db_index=True, default=uuid.uuid4, unique=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("ready", "Ready to send"),
                            ("sent", "Sent"),
                            ("viewed", "Viewed"),
                            ("accepted", "Accepted"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=20,
                    ),
                ),
                (
                    "intro_message",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Shown at top of the corporate shortlist page",
                        max_length=8000,
                    ),
                ),
                (
                    "internal_notes",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Internal-only; not exposed on public shortlist",
                        max_length=8000,
                    ),
                ),
                ("deposit_percent", models.PositiveSmallIntegerField(default=25)),
                ("currency", models.CharField(default="usd", max_length=3)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("first_viewed_at", models.DateTimeField(blank=True, null=True)),
                ("last_viewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "inquiry",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shortlist",
                        to="quickstart.corporateinquiry",
                    ),
                ),
            ],
            options={
                "db_table": "corporate_shortlists",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="CorporateBooking",
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
                    "reference",
                    models.CharField(
                        db_index=True,
                        default=_default_corp_ref,
                        max_length=32,
                        unique=True,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending_deposit", "Pending deposit"),
                            ("deposit_paid", "Deposit paid"),
                            ("invoiced", "Balance invoiced"),
                            ("fully_paid", "Fully paid"),
                            ("in_progress", "In progress"),
                            ("completed", "Completed"),
                            ("cancelled", "Cancelled"),
                            ("refunded", "Refunded"),
                        ],
                        db_index=True,
                        default="pending_deposit",
                        max_length=32,
                    ),
                ),
                ("headcount", models.PositiveIntegerField()),
                ("confirmed_datetime", models.DateTimeField()),
                (
                    "special_requests",
                    models.TextField(blank=True, default="", max_length=8000),
                ),
                ("billing_company_name", models.CharField(max_length=200)),
                ("billing_contact_name", models.CharField(max_length=150)),
                ("billing_email", models.EmailField(max_length=254)),
                ("billing_address", models.JSONField(blank=True, default=dict)),
                ("po_number", models.CharField(blank=True, default="", max_length=100)),
                ("total_cents", models.PositiveIntegerField()),
                ("deposit_cents", models.PositiveIntegerField()),
                ("balance_cents", models.PositiveIntegerField()),
                ("currency", models.CharField(default="usd", max_length=3)),
                (
                    "stripe_customer_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "deposit_payment_intent_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("deposit_paid_at", models.DateTimeField(blank=True, null=True)),
                (
                    "stripe_invoice_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "invoice_status",
                    models.CharField(blank=True, default="", max_length=50),
                ),
                (
                    "invoice_url",
                    models.URLField(blank=True, default="", max_length=2000),
                ),
                ("invoice_due_at", models.DateTimeField(blank=True, null=True)),
                ("balance_paid_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "shortlist",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bookings",
                        to="quickstart.corporateshortlist",
                    ),
                ),
            ],
            options={
                "db_table": "corporate_bookings",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="CorporateShortlistOption",
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
                    "position",
                    models.PositiveSmallIntegerField(
                        help_text="Display order 1-3 on the shortlist"
                    ),
                ),
                (
                    "source_type",
                    models.CharField(
                        choices=[
                            ("existing_class", "Existing class"),
                            ("custom", "Custom"),
                        ],
                        default="custom",
                        max_length=20,
                    ),
                ),
                ("title", models.CharField(max_length=200)),
                ("host_name", models.CharField(blank=True, default="", max_length=200)),
                (
                    "tagline",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                (
                    "description",
                    models.TextField(blank=True, default="", max_length=8000),
                ),
                ("inclusions", models.JSONField(blank=True, default=list)),
                (
                    "cover_image_url",
                    models.URLField(blank=True, default="", max_length=2000),
                ),
                ("gallery_urls", models.JSONField(blank=True, default=list)),
                (
                    "location_text",
                    models.CharField(blank=True, default="", max_length=500),
                ),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("min_headcount", models.PositiveIntegerField(blank=True, null=True)),
                ("max_headcount", models.PositiveIntegerField(blank=True, null=True)),
                ("price_total_cents", models.PositiveIntegerField()),
                (
                    "price_per_person_cents",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "proposed_date_options",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text="List of ISO datetime strings the corp may choose from",
                    ),
                ),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "shortlist",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="options",
                        to="quickstart.corporateshortlist",
                    ),
                ),
                (
                    "source_class",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="corporate_shortlist_options",
                        to="quickstart.classesmain",
                    ),
                ),
            ],
            options={
                "db_table": "corporate_shortlist_options",
                "ordering": ["position", "created_at"],
            },
        ),
        migrations.AddField(
            model_name="corporatebooking",
            name="selected_option",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="bookings",
                to="quickstart.corporateshortlistoption",
            ),
        ),
        migrations.AddConstraint(
            model_name="corporateshortlistoption",
            constraint=models.UniqueConstraint(
                fields=("shortlist", "position"),
                name="uniq_corporate_shortlist_option_position",
            ),
        ),
        migrations.CreateModel(
            name="CorporateBookingEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("message", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="quickstart.corporatebooking",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "corporate_booking_events",
                "ordering": ["-created_at"],
            },
        ),
    ]
