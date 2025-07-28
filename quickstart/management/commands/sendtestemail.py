# quickstart/management/commands/sendtestemail.py

import json
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from quickstart.utils.email_utils import (
    send_templated_email,
)  # Adjust import path as needed
from quickstart.models import CustomUser, Booking  # Import models for mock data


class Command(BaseCommand):
    help = "Sends a test email using a specified HTML template."

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient_email",
            type=str,
            help="The email address to send the test email to.",
        )
        parser.add_argument(
            "template_name",
            type=str,
            help='The path to the email template, e.g., "emails/welcome_user.html".',
        )
        parser.add_argument(
            "--context-json",
            type=str,
            help="A JSON string representing the context dictionary for the template.",
            default="{}",
        )
        parser.add_argument(
            "--subject",
            type=str,
            help="Optional subject line. If not provided, it will be derived from the template.",
            default=None,
        )

    def handle(self, *args, **options):
        recipient_email = options["recipient_email"]
        template_name = options["template_name"]
        subject = options["subject"]

        self.stdout.write(
            f"Preparing to send test email to '{recipient_email}' using template '{template_name}'..."
        )

        try:
            context = json.loads(options["context_json"])
            self.stdout.write(f"Using provided context: {context}")
        except json.JSONDecodeError:
            raise CommandError("Invalid JSON provided for --context-json argument.")

        # --- Add default or mock context for common templates ---
        # This makes testing much faster as you don't have to provide all the context every time.
        if "user" not in context:
            # Use the recipient's email to find a user or create a mock one
            user, created = CustomUser.objects.get_or_create(
                email=recipient_email,
                defaults={"first_name": "Test", "last_name": "User"},
            )
            context["user"] = user
            self.stdout.write(
                self.style.NOTICE(
                    f"Mock 'user' object added to context for {user.email}."
                )
            )

        if "booking" not in context and "booking_confirmation" in template_name:
            # Find any booking to use as a mock, or create a mock object
            booking = Booking.objects.order_by("?").first()
            if booking:
                context["booking"] = booking
                self.stdout.write(
                    self.style.NOTICE(
                        f"Mock 'booking' object (ID: {booking.id}) added to context."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "Could not find a real booking to use as mock data."
                    )
                )

        # Ensure required base context variables are present
        context.setdefault("recipient_email", recipient_email)
        context.setdefault("frontend_base_url", settings.FRONTEND_BASE_URL)
        context.setdefault("settings", settings)

        try:
            send_templated_email(
                recipient_list=[recipient_email],
                template_name=template_name,
                context=context,
                subject=subject,
            )
            self.stdout.write(
                self.style.SUCCESS(f"✅ Successfully queued test email for sending.")
            )
        except Exception as e:
            raise CommandError(
                f"❌ An error occurred while trying to send the email: {e}"
            )
