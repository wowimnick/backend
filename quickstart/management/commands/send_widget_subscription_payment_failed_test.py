"""
Send the "widget subscription payment failed" email to a given address for testing.
Uses mock data; no DB records created. Uses CELERY_TASK_ALWAYS_EAGER so the email sends immediately.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import override_settings

from quickstart.models import CustomUser
from quickstart.utils.email_utils import send_widget_subscription_payment_failed_email


class Command(BaseCommand):
    help = (
        "Send the widget subscription payment failed (update your card) email to a given "
        "email address for testing. Uses mock user and business/plan names."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            type=str,
            help="Email address to send the test email to.",
        )
        parser.add_argument(
            "--business-name",
            type=str,
            default="Demo Yoga Studio",
            dest="business_name",
            help="Business name to show in the email (default: Demo Yoga Studio).",
        )
        parser.add_argument(
            "--plan",
            type=str,
            default="Growth",
            help="Plan name to show (default: Growth).",
        )
        parser.add_argument(
            "--grace-days",
            type=int,
            default=7,
            dest="grace_days",
            help="Grace period in days (default: 7).",
        )
        parser.add_argument(
            "--first-name",
            type=str,
            default="Partner",
            dest="first_name",
            help="Recipient first name for greeting (default: Partner).",
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def handle(self, *args, **options):
        email = options["email"]
        business_name = options["business_name"] or "Demo Business"
        plan_name = options["plan"] or "Growth"
        grace_days = options["grace_days"] or 7
        first_name = options["first_name"] or "Partner"

        user = CustomUser(
            userId=0,
            email=email,
            first_name=first_name,
            username=f"test_widget_payment_fail_{email.replace('@', '_')}",
        )

        settings_billing_url = f"{getattr(settings, 'FRONTEND_BASE_URL', '') or 'https://app.classeasily.com'}/business/dashboard?tab=settings"

        self.stdout.write(
            self.style.WARNING(
                f"Sending widget subscription payment failed email to {email} "
                f"(business={business_name}, plan={plan_name}, grace_days={grace_days})"
            )
        )

        send_widget_subscription_payment_failed_email(
            business_user=user,
            business_name=business_name,
            plan_name=plan_name,
            settings_billing_url=settings_billing_url,
            grace_days=grace_days,
        )

        self.stdout.write(self.style.SUCCESS(f"Email queued/sent to {email}."))
