# quickstart/management/commands/sendtestemail.py

import json
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from quickstart.utils.email_utils import (
    send_templated_email,
    _get_booking_related_data,  # Import the helper to generate real data
)
from quickstart.models import CustomUser, Booking, CourseEnrollment


class Command(BaseCommand):
    help = "Sends a test email using a specified HTML template with smart context generation."

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient_email",
            type=str,
            help="The email address to send the test email to.",
        )
        parser.add_argument(
            "template_name",
            type=str,
            help='The path to the email template, e.g., "emails/course_confirmation_user.html".',
        )
        parser.add_argument(
            "--booking-id",
            type=int,
            help="The ID of a specific booking to use for context data. Highly recommended.",
            default=None,
        )
        parser.add_argument(
            "--context-json",
            type=str,
            help="A JSON string representing additional context variables.",
            default="{}",
        )
        parser.add_argument(
            "--subject",
            type=str,
            help="Optional subject line.",
            default=None,
        )

    def handle(self, *args, **options):
        recipient_email = options["recipient_email"]
        template_name = options["template_name"]
        booking_id = options["booking_id"]
        subject = options["subject"]

        self.stdout.write(f"Preparing to send test email to '{recipient_email}'...")

        # 1. Parse manual JSON context
        try:
            context = json.loads(options["context_json"])
        except json.JSONDecodeError:
            raise CommandError("Invalid JSON provided for --context-json argument.")

        # 2. Get or Create User
        user, created = CustomUser.objects.get_or_create(
            email=recipient_email,
            defaults={"first_name": "Test", "last_name": "User"},
        )
        if "user" not in context:
            context["user"] = user

        # 3. Intelligent Booking Context Injection
        booking = None
        
        # If ID provided, fetch it
        if booking_id:
            try:
                booking = Booking.objects.get(pk=booking_id)
                self.stdout.write(self.style.NOTICE(f"Using Booking ID: {booking.id}"))
            except Booking.DoesNotExist:
                raise CommandError(f"Booking with ID {booking_id} not found.")
        
        # If no ID, but template implies booking, try to find a random one
        elif any(x in template_name for x in ["booking", "course", "reminder", "cancellation"]):
            booking = Booking.objects.order_by("-id").first()
            if booking:
                self.stdout.write(self.style.WARNING(f"No booking-id provided. Auto-selected most recent Booking ID: {booking.id}"))
            else:
                self.stdout.write(self.style.ERROR("No bookings found in database to populate context."))

        # 4. Populate Complex Data (The missing piece in your original script)
        if booking:
            context["booking"] = booking
            
            # Generate the rich data (Class Title, Location, Timezone, etc.)
            # This is what populates {{ related_data.class_title }} in the templates
            related_data = _get_booking_related_data(booking)
            context["related_data"] = related_data
            
            # Standard URLs
            class_identifier = related_data.get("class_slug") or related_data.get("class_id")
            context["class_details_url"] = f"{settings.FRONTEND_BASE_URL}/classes/{class_identifier}" if class_identifier else "#"
            context["manage_bookings_url"] = f"{settings.FRONTEND_BASE_URL}/my-classes"
            context["explore_url"] = f"{settings.FRONTEND_BASE_URL}/explore"

            # Course Specific Data
            if booking.enrollment_type == "Full Course":
                # If checking a course template, try to get the enrollment object
                if booking.booking_group_id:
                    enrollment = CourseEnrollment.objects.filter(booking_group_id=booking.booking_group_id).first()
                    if enrollment:
                        context["enrollment"] = enrollment
                        self.stdout.write(self.style.NOTICE(f"Added CourseEnrollment ID: {enrollment.id} to context."))
                
                # For reminder emails
                if "reminder" in template_name:
                    context["is_course_session"] = True

        # 5. Base Context
        context.setdefault("recipient_email", recipient_email)
        context.setdefault("settings", settings)

        # 6. Send
        try:
            send_templated_email(
                recipient_list=[recipient_email],
                template_name=template_name,
                context=context,
                subject=subject,
            )
            self.stdout.write(self.style.SUCCESS(f"✅ Email queued successfully!"))
        except Exception as e:
            import traceback
            self.stdout.write(self.style.ERROR(traceback.format_exc()))
            raise CommandError(f"❌ Failed to send email: {e}")