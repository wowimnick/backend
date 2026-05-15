# Generated manually (makemigrations unavailable in this environment)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0237_search_relevance_and_schedule_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="contact",
            name="stripe_customer_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Stripe Customer id for this contact (membership subscriptions).",
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="widgetsubscription",
            name="payment_grace_until",
            field=models.DateTimeField(
                blank=True,
                help_text="While status is past_due, platform access may continue until this timestamp.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="widgetsubscription",
            name="comp_reason",
            field=models.TextField(
                blank=True,
                default="",
                help_text="If set, subscription was comped/overridden by admin (no Stripe subscription).",
            ),
        ),
        migrations.AlterModelOptions(
            name="businessinfo",
            options={
                "verbose_name_plural": "Business Information",
                "permissions": [
                    (
                        "toggle_business_feature",
                        "Can toggle the featured status for any business",
                    ),
                    (
                        "manage_business_staff",
                        "Can invite, remove, and manage staff for own business",
                    ),
                    (
                        "view_business_metrics",
                        "Can view aggregated business management statistics",
                    ),
                    ("export_business_data", "Can export business data as CSV"),
                    (
                        "send_business_announcements",
                        "Can send platform announcements to businesses",
                    ),
                    ("access_business_admin", "Can access the Business Administration section"),
                    (
                        "manage_own_classes",
                        "Can create/edit classes, options, schedules for own business",
                    ),
                    (
                        "manage_own_schedule_instances",
                        "Can manage instances (attendance, cancel) for own classes",
                    ),
                    ("view_own_business_bookings", "Can view bookings for own business"),
                    ("manage_own_business_profile", "Can edit own business profile details"),
                    (
                        "access_business_dashboard",
                        "Can access the dashboard for managing their own business",
                    ),
                    (
                        "view_business_revenue_analytics",
                        "Can view revenue analytics for own business",
                    ),
                    (
                        "export_business_revenue_data",
                        "Can export revenue data for own business",
                    ),
                    (
                        "view_business_students",
                        "Can view students associated with own business",
                    ),
                    (
                        "add_studentnote",
                        "Can add notes to students associated with own business",
                    ),
                    (
                        "view_studentnote",
                        "Can view notes for students associated with own business",
                    ),
                    ("manage_business_roles", "Can create, edit, and manage staff roles"),
                    (
                        "manage_own_business_discounts",
                        "Can create, edit, and manage discounts",
                    ),
                    (
                        "receive_booking_notifications",
                        "Can receive business notifications for new bookings and cancellations",
                    ),
                    (
                        "manage_email_marketing",
                        "Can create and send email marketing campaigns for own business",
                    ),
                    (
                        "view_business_members",
                        "Can view membership products and members for own business",
                    ),
                    (
                        "manage_business_members",
                        "Can create, approve, and cancel memberships for own business",
                    ),
                ],
            },
        ),
        migrations.AlterModelOptions(
            name="widgetsubscription",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    (
                        "view_widgetsubscription",
                        "Can view widget subscription admin data",
                    ),
                ],
            },
        ),
    ]
