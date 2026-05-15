# Extend BusinessRole.permissions limit_choices for membership permissions

import django.db.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0238_widget_grace_contact_stripe_and_perms"),
    ]

    operations = [
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
                        "view_business_members",
                        "manage_business_members",
                    ],
                },
                to="auth.permission",
            ),
        ),
    ]
