# quickstart/management/commands/send_test_email_branding.py
"""
Send test emails to compare original (default ClassEasily) vs custom email branding.
Sends two emails: one without branding, one with custom branding (from a business or sample).
"""
from datetime import date, time, timedelta
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from django.utils import timezone

from quickstart.models import BusinessInfo, Booking, CustomUser
from quickstart.utils.email_utils import send_templated_email, _get_booking_related_data


# Sample branding used when no --business-id is provided (so you can test without a business)
SAMPLE_EMAIL_BRANDING = {
    "logo_url": "https://d1uuoquc68y10e.cloudfront.net/public/sig.png",
    "primary_color": "#0d9488",
    "footer_text": "Questions? Reply to this email or visit our help center.",
    "confirmation_message": "We can't wait to see you — bring a mat and water!",
    "card_border_width": 1,
    "card_border_radius": 24,
    "logo_max_width": 160,
    "logo_max_height": 60,
}


def _build_base_context(booking, user, related_data):
    """Build context shared by both original and branded emails."""
    formatted_time_range = "9:00 AM – 10:00 AM"
    if hasattr(booking, "schedule_instance") and getattr(booking.schedule_instance, "time", None):
        t = booking.schedule_instance.time
        if hasattr(t, "strftime"):
            formatted_time_range = t.strftime("%-I:%M %p") + " – " + (t.strftime("%-I:%M %p") if not getattr(booking.schedule_instance, "duration", None) else "10:00 AM")
    duration = getattr(getattr(booking, "schedule_instance", None), "duration", 60)
    formatted_duration_minutes = f"{duration} min" if duration else None
    tz_display = (related_data.get("business_timezone") or "UTC").replace("_", " ")

    return {
        "user": user,
        "booking": booking,
        "related_data": related_data,
        "manage_bookings_url": f"{settings.FRONTEND_BASE_URL}/my-classes",
        "class_details_url": f"{settings.FRONTEND_BASE_URL}/classes/{related_data.get('class_slug', 'sample')}",
        "recipient_email": user.email,
        "formatted_time_range": formatted_time_range,
        "formatted_timezone_display": tz_display,
        "formatted_duration_minutes": formatted_duration_minutes,
        "is_guest": False,
        "payment": None,
    }


def _build_mock_booking_and_data(user):
    """Build minimal mock booking and related_data when no real booking is used."""
    tomorrow = (timezone.now() + timedelta(days=1)).date()
    mock_instance = SimpleNamespace(
        date=tomorrow,
        time=time(9, 0),
        duration=60,
    )
    mock_booking = SimpleNamespace(
        id=0,
        user_facing_reference="TEST-001",
        schedule_instance=mock_instance,
        participants=1,
        participant_details=[],
        user=user,
        contact=user,  # template uses booking.user|default:booking.contact for "Booked by"
    )
    related_data = {
        "class_title": "Morning Yoga Flow",
        "class_slug": "morning-yoga-flow",
        "class_location": "123 Studio Lane, Toronto",
        "business_name": "Demo Yoga Studio",
        "business_timezone": "America/Toronto",
        "has_multiple_options": False,
        "option_title": "General Admission",
        "business_contact_email": "hello@demoyoga.com",
        "business_contact_phone": None,
        "equipment": "",
        "cancellation_policy_display": "Free cancellation up to 24 hours before the start time.",
    }
    return mock_booking, related_data


def _apply_branding_to_context(context, branding, header_logo_url=None, header_logo_max_width=160, header_logo_max_height=60):
    """Add email_branding and optional header logo to context."""
    context = dict(context)
    if branding:
        context["email_branding"] = branding
    if header_logo_url:
        context["header_logo_url"] = header_logo_url
        context["header_logo_max_width"] = header_logo_max_width
        context["header_logo_max_height"] = header_logo_max_height
    return context


class Command(BaseCommand):
    help = (
        "Send two test emails (original and custom branded) to compare default "
        "ClassEasily styling vs your email branding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            type=str,
            help="Email address to send the test emails to.",
        )
        parser.add_argument(
            "--booking-id",
            type=int,
            default=None,
            help="Use a real booking from the DB for context (recommended). Otherwise uses mock data.",
        )
        parser.add_argument(
            "--business-id",
            type=int,
            default=None,
            help="Use this business's marketplace email branding for the 'custom' email. If omitted, uses a sample branding.",
        )
        parser.add_argument(
            "--template",
            type=str,
            default="emails/booking_confirmation_user.html",
            help="Template to render (default: booking confirmation).",
        )
        parser.add_argument(
            "--original-only",
            action="store_true",
            help="Send only the original (no branding) email.",
        )
        parser.add_argument(
            "--custom-only",
            action="store_true",
            help="Send only the custom branded email.",
        )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
    def handle(self, *args, **options):
        email = options["email"]
        booking_id = options["booking_id"]
        business_id = options["business_id"]
        template = options["template"]
        original_only = options["original_only"]
        custom_only = options["custom_only"]

        if original_only and custom_only:
            raise CommandError("Use at most one of --original-only and --custom-only.")

        user, _ = CustomUser.objects.get_or_create(
            email=email,
            defaults={"first_name": "Test", "last_name": "User", "username": email.replace("@", "_")},
        )

        if booking_id:
            try:
                booking = Booking.objects.select_related(
                    "schedule_instance__schedule__option__classId__businessId"
                ).get(pk=booking_id)
            except Booking.DoesNotExist:
                raise CommandError(f"Booking with id {booking_id} not found.")
            related_data = _get_booking_related_data(booking)
            self.stdout.write(self.style.NOTICE(f"Using real booking id={booking_id}."))
        else:
            booking, related_data = _build_mock_booking_and_data(user)
            self.stdout.write(self.style.WARNING("Using mock booking data (pass --booking-id for real data)."))

        base_context = _build_base_context(booking, user, related_data)
        base_context["settings"] = settings

        # Resolve branding for "custom" email
        custom_branding = None
        header_logo_url = None
        header_logo_max_width = 160
        header_logo_max_height = 60
        if business_id:
            try:
                business = BusinessInfo.objects.get(pk=business_id)
                custom_branding = getattr(business, "marketplace_email_branding", None) or {}
                if custom_branding:
                    logo_url = (custom_branding.get("logo_url") or "").strip()
                    if logo_url:
                        header_logo_url = logo_url
                    try:
                        w, h = custom_branding.get("logo_max_width"), custom_branding.get("logo_max_height")
                        if w not in (None, ""):
                            header_logo_max_width = int(w)
                        if h not in (None, ""):
                            header_logo_max_height = int(h)
                    except (TypeError, ValueError):
                        pass
                self.stdout.write(self.style.NOTICE(f"Using business id={business_id} branding."))
            except BusinessInfo.DoesNotExist:
                raise CommandError(f"Business with id {business_id} not found.")
        else:
            custom_branding = SAMPLE_EMAIL_BRANDING
            header_logo_url = (custom_branding.get("logo_url") or "").strip() or None
            header_logo_max_width = custom_branding.get("logo_max_width") or 160
            header_logo_max_height = custom_branding.get("logo_max_height") or 60
            self.stdout.write(self.style.NOTICE("Using sample branding (pass --business-id to use a business's branding)."))

        sent = 0

        if not custom_only:
            ctx_original = dict(base_context)
            send_templated_email(
                recipient_list=[email],
                template_name=template,
                context=ctx_original,
                subject="[Original] You're confirmed!",
            )
            sent += 1
            self.stdout.write(self.style.SUCCESS("Sent [Original] email (no branding)."))

        if not original_only:
            ctx_custom = _apply_branding_to_context(
                base_context, custom_branding, header_logo_url, header_logo_max_width, header_logo_max_height
            )
            send_templated_email(
                recipient_list=[email],
                template_name=template,
                context=ctx_custom,
                subject="[Custom branding] You're confirmed!",
            )
            sent += 1
            self.stdout.write(self.style.SUCCESS("Sent [Custom branding] email."))

        self.stdout.write(self.style.SUCCESS(f"Done. {sent} email(s) sent to {email}."))
