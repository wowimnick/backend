"""
Send all 5 transactional SMS types (no campaign) to a given phone number.
Uses send_sms() directly — Celery is not required.
With --class-id, uses real class title and a random future schedule so variables match prod.
Set AWS_SMS_ENABLED=true and AWS_SNS_REGION (e.g. us-east-2). Local: AWS credentials required.
"""
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from quickstart.utils.sms_utils import send_sms, normalize_phone_for_sns
from quickstart.models import ClassesMain, ScheduleInstance


class Command(BaseCommand):
    help = "Send one of each transactional SMS type to a given phone number (no Celery required). Use --class-id to test with real class and future schedule."

    def add_arguments(self, parser):
        parser.add_argument(
            "phone",
            type=str,
            help="Phone number (e.g. +15551234567 or 5551234567)",
        )
        parser.add_argument(
            "--class-id",
            type=int,
            default=None,
            metavar="ID",
            help="Class ID (ClassesMain PK). Picks a random future schedule for this class and uses its title/date/time in messages.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print messages; do not send.",
        )

    def _get_class_and_instance(self, class_id):
        """Return (class_title, date_str, time_str, business_name) from a future instance for the class, or (None, None, None, None)."""
        try:
            cls = ClassesMain.objects.get(pk=class_id)
        except ClassesMain.DoesNotExist:
            return None, None, None, None
        business_name = ""
        if getattr(cls, "businessId", None):
            business_name = getattr(cls.businessId, "businessName", "") or ""
        today = timezone.now().date()
        instance = (
            ScheduleInstance.objects.filter(
                schedule__option__classId=cls,
                date__gte=today,
                status="scheduled",
            )
            .order_by("?")
            .first()
        )
        if not instance:
            return getattr(cls, "title", "Class"), None, None, business_name
        date_str = instance.date.strftime("%b %d")
        t = instance.time
        time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
        return getattr(cls, "title", "Class"), date_str, time_str, business_name

    def handle(self, *args, **options):
        phone = options["phone"].strip()
        dry_run = options["dry_run"]
        class_id = options.get("class_id")

        normalized = normalize_phone_for_sns(phone)
        if not normalized:
            self.stderr.write(self.style.ERROR(f"Invalid phone: {phone}"))
            return

        if not getattr(settings, "AWS_SMS_ENABLED", False) and not dry_run:
            self.stderr.write(
                self.style.ERROR("AWS_SMS_ENABLED is not true. Set it to enable SMS.")
            )
            return

        class_title = "Advanced Vinyasa Flow"
        date_str = "Mar 15"
        time_str = "9:00 AM"
        business_name = "Studio Name"
        booker_name = "A customer"
        host_message = ""
        if class_id:
            class_title, date_str, time_str, business_name = self._get_class_and_instance(class_id)
            if not class_title:
                self.stderr.write(self.style.ERROR(f"Class ID {class_id} not found."))
                return
            if not business_name:
                business_name = "Studio Name"
            if date_str and time_str:
                self.stdout.write(
                    self.style.WARNING(f"Using class: {class_title}, date: {date_str}, time: {time_str}, business: {business_name}")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"Class '{class_title}' has no future scheduled instances; using placeholder date/time."
                    )
                )
                date_str = date_str or "Mar 15"
                time_str = time_str or "9:00 AM"
        else:
            self.stdout.write(
                self.style.WARNING("No --class-id given; using placeholder class/date/time.")
            )

        region = getattr(settings, "AWS_SNS_REGION", "us-east-2")
        self.stdout.write(
            self.style.WARNING(f"Target: {normalized} (region: {region}, dry_run={dry_run})")
        )

        # 1C, 2A (+ dashboard), 3C (+ host message if any), 4A, 5A
        reminder_body = f"Heads up — {class_title} is tomorrow, {date_str} at {time_str}.\n\nNeed to cancel? Do it from your booking.\n\n— {business_name}"
        if host_message and host_message.strip():
            reminder_body = f"{reminder_body}\n\nFrom your host: {host_message.strip()}"
        messages = [
            ("1. Booking confirmation (booker)", f"You're in! {class_title} is on {date_str} at {time_str}.\n\nAdd it to your calendar — we'll send a reminder the day before.\n\n— {business_name}"),
            ("2. New booking alert (business)", f"New booking: {class_title} on {date_str} at {time_str}.\n\nBooked by {booker_name}. Check your dashboard for details.\n\n— ClassEasily"),
            ("3. Booking reminder (booker)", reminder_body),
            ("4. Cancellation – booker", f"Your booking for {class_title} on {date_str} at {time_str} has been cancelled.\n\nIf you paid, you'll receive a refund.\n\n— {business_name}"),
            ("5. Cancellation – business", f"A booking was cancelled: {class_title} on {date_str} at {time_str}.\n\nSpot is available again.\n\n— ClassEasily"),
        ]

        for label, body in messages:
            if dry_run:
                self.stdout.write(f"  [dry-run] {label}: {body[:60]}...")
                continue
            ok = send_sms(normalized, body)
            if ok:
                self.stdout.write(self.style.SUCCESS(f"  Sent: {label}"))
            else:
                self.stdout.write(self.style.ERROR(f"  Failed: {label}"))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS("Done."))
