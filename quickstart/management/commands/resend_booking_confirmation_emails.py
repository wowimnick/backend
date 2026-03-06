# quickstart/management/commands/resend_booking_confirmation_emails.py
"""
Resend booking confirmation emails to a specific email address.

Use after fixing Apple Pay placeholder contacts: the contact now has the correct
email; this sends the confirmation to that address (only for the email you specify).

Usage:
  python manage.py resend_booking_confirmation_emails customer@example.com
  python manage.py resend_booking_confirmation_emails customer@example.com --dry-run
  python manage.py resend_booking_confirmation_emails customer@example.com --dry-run-to you@example.com
"""

from django.core.management.base import BaseCommand
from django.test import override_settings

from quickstart.models import Booking, Contact, CustomUser
from quickstart.utils.email_utils import send_booking_confirmation_email


class Command(BaseCommand):
    help = (
        "Resend booking confirmation email(s) to a specific address. "
        "Finds the contact (or user) with that email and resends for each of their confirmed/completed bookings."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            type=str,
            help="Email address to resend to (e.g. the corrected contact email).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List which emails would be sent without sending.",
        )
        parser.add_argument(
            "--dry-run-to",
            type=str,
            metavar="EMAIL",
            help="Send the real confirmation email to this address instead of the booking email (to verify content).",
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def handle(self, *args, **options):
        email = (options["email"] or "").strip().lower()
        if not email:
            self.stdout.write(self.style.ERROR("Please provide an email address."))
            return

        dry_run = options["dry_run"]
        dry_run_to = (options.get("dry_run_to") or "").strip()
        if dry_run and dry_run_to:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN (send to test address): Emails will be sent to %s only, not to the booking email."
                    % dry_run_to
                )
            )
        elif dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN: No emails will be sent.")
            )

        # Find contact(s) with this email (each business can have one contact per email)
        contacts = Contact.objects.filter(email__iexact=email)
        # Find user with this email (optional)
        user = CustomUser.objects.filter(email__iexact=email).first()

        # Bookings to resend: confirmed/completed, where recipient is this contact or this user
        bookings_to_send = []
        if user:
            bookings_to_send.extend(
                Booking.objects.filter(
                    user=user,
                    status__in=["confirmed", "completed"],
                ).select_related("schedule_instance__schedule__option__classId", "contact")
            )
        for contact in contacts:
            bookings_to_send.extend(
                Booking.objects.filter(
                    contact=contact,
                    status__in=["confirmed", "completed"],
                ).select_related("schedule_instance__schedule__option__classId", "contact")
            )

        # Deduplicate by booking id (user and contact could theoretically share a booking)
        seen = set()
        unique_bookings = []
        for b in bookings_to_send:
            if b.id not in seen:
                seen.add(b.id)
                unique_bookings.append(b)

        if not unique_bookings:
            self.stdout.write(
                self.style.WARNING(
                    f"No confirmed/completed bookings found for email: {email}"
                )
            )
            return

        send_to_test = dry_run_to if dry_run_to else None
        self.stdout.write(
            f"Found {len(unique_bookings)} booking(s) for {email}. "
            + (
                "Would resend (no send):"
                if dry_run and not send_to_test
                else "Sending confirmation email(s)"
                + (f" to {send_to_test} (test):" if send_to_test else ":")
            )
        )

        for booking in unique_bookings:
            recipient = booking.user or booking.contact
            if not recipient or not getattr(recipient, "email", None):
                self.stdout.write(
                    self.style.ERROR(
                        f"  Booking {booking.id} (ref {booking.user_facing_reference}): no recipient."
                    )
                )
                continue

            if dry_run and not send_to_test:
                self.stdout.write(
                    f"  Would send to {recipient.email} for booking {booking.id} (ref {booking.user_facing_reference})"
                )
                continue

            try:
                override_list = [send_to_test] if send_to_test else None
                send_booking_confirmation_email(
                    recipient, booking, override_recipient_list=override_list
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Sent to {send_to_test or recipient.email} for booking {booking.id} (ref {booking.user_facing_reference})"
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  Failed booking {booking.id}: {e}"
                    )
                )

        self.stdout.write("-" * 60)
        self.stdout.write("Done.")
