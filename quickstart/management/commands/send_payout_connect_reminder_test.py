"""
Send the "Connect your account to receive your payout" email to a given address for testing.
Uses in-memory user and sample amounts; no DB records created.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.test import override_settings

from quickstart.models import CustomUser
from quickstart.utils import email_utils


class Command(BaseCommand):
    help = (
        "Send the payout connect required (connect Stripe) reminder email to a given "
        "email address for testing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            type=str,
            help="Email address to send the test email to.",
        )
        parser.add_argument(
            "--amount",
            type=str,
            default="247.50",
            help="Pending amount to show in the email (default: 247.50)",
        )
        parser.add_argument(
            "--booking-count",
            type=int,
            default=5,
            dest="booking_count",
            help="Number of bookings to show (default: 5)",
        )
        parser.add_argument(
            "--currency",
            type=str,
            default="CAD",
            help="Currency code (default: CAD)",
        )
        parser.add_argument(
            "--first-name",
            type=str,
            default="Partner",
            dest="first_name",
            help="Recipient first name for greeting (default: Partner)",
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def handle(self, *args, **options):
        email = options["email"]
        pending_amount = Decimal(options["amount"])
        booking_count = options["booking_count"]
        currency = (options["currency"] or "CAD").upper()
        first_name = options["first_name"] or "Partner"

        user = CustomUser(
            userId=0,
            email=email,
            first_name=first_name,
            username=f"test_payout_reminder_{email.replace('@', '_')}",
        )

        self.stdout.write(
            self.style.WARNING(
                f"Sending payout connect required email to {email} "
                f"(amount={pending_amount} {currency}, booking_count={booking_count})"
            )
        )

        email_utils.send_payout_connect_required_email(
            business_user=user,
            pending_amount=pending_amount,
            booking_count=booking_count,
            currency=currency,
        )

        self.stdout.write(self.style.SUCCESS(f"Email queued/sent to {email}."))
