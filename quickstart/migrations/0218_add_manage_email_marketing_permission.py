"""
Add manage_email_marketing custom permission to BusinessInfo, and extend
BusinessRole.permissions limit_choices_to to include it.
"""

import django.db.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0217_marketing_segments_workflows"),
    ]

    operations = [
        # 1. Add the new permission to BusinessInfo Meta.permissions
        migrations.AlterModelOptions(
            name="businessinfo",
            options={
                "verbose_name_plural": "Business Information",
                "permissions": [
                    ("toggle_business_feature", "Can toggle the featured status for any business"),
                    ("manage_business_staff", "Can invite, remove, and manage staff for own business"),
                    ("view_business_metrics", "Can view aggregated business management statistics"),
                    ("export_business_data", "Can export business data as CSV"),
                    ("send_business_announcements", "Can send platform announcements to businesses"),
                    ("access_business_admin", "Can access the Business Administration section"),
                    ("manage_own_classes", "Can create/edit classes, options, schedules for own business"),
                    ("manage_own_schedule_instances", "Can manage instances (attendance, cancel) for own classes"),
                    ("view_own_business_bookings", "Can view bookings for own business"),
                    ("manage_own_business_profile", "Can edit own business profile details"),
                    ("access_business_dashboard", "Can access the dashboard for managing their own business"),
                    ("view_business_revenue_analytics", "Can view revenue analytics for own business"),
                    ("export_business_revenue_data", "Can export revenue data for own business"),
                    ("view_business_students", "Can view students associated with own business"),
                    ("add_studentnote", "Can add notes to students associated with own business"),
                    ("view_studentnote", "Can view notes for students associated with own business"),
                    ("manage_business_roles", "Can create, edit, and manage staff roles"),
                    ("manage_own_business_discounts", "Can create, edit, and manage discounts"),
                    ("receive_booking_notifications", "Can receive business notifications for new bookings and cancellations"),
                    ("manage_email_marketing", "Can create and send email marketing campaigns for own business"),
                ],
            },
        ),
        # 2. Extend BusinessRole.permissions limit_choices_to
        migrations.AlterField(
            model_name="businessrole",
            name="permissions",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={
                    "content_type__app_label": "quickstart",
                    "codename__in": [
                        "manage_own_classes",
                        "manage_own_schedule_instances",
                        "view_own_business_bookings",
                        "manage_own_business_profile",
                        "manage_business_staff",
                        "manage_business_roles",
                        "view_business_revenue_analytics",
                        "export_business_revenue_data",
                        "view_business_students",
                        "add_studentnote",
                        "view_studentnote",
                        "view_own_booking_analytics",
                        "cancel_business_booking",
                        "view_own_business_reviews",
                        "add_business_review_response",
                        "manage_own_business_discounts",
                        "access_business_dashboard",
                        "receive_booking_notifications",
                        "manage_email_marketing",
                    ],
                },
                to="auth.permission",
            ),
        ),
    ]
