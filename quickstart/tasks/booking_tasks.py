# quickstart/tasks/booking_tasks.py
import pytz
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache

from quickstart.models import Booking
from quickstart.utils.email_utils import send_booking_reminder_email
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_upcoming_booking_reminders():
    """
    Sends reminder emails for confirmed bookings scheduled to start
    within the next 23 to 24 hours. This version uses a more precise
    database query to handle midnight edge cases correctly.
    """
    now = timezone.now()
    reminder_start_time = now + timedelta(hours=23)
    reminder_end_time = now + timedelta(hours=24)

    # --- NEW, MORE PRECISE QUERY ---
    # The reminder window can span two different dates (e.g., from 23:30 to 00:30).
    # This gets the unique dates the window covers.
    possible_dates = {reminder_start_time.date(), reminder_end_time.date()}

    # Fetch all confirmed bookings on those specific dates.
    # This is more efficient than a broad date range.
    upcoming_bookings = (
        Booking.objects.filter(
            status="confirmed",
            schedule_instance__date__in=possible_dates,
        )
        .select_related(
            "user",
            "contact",  # Added contact
            "schedule_instance__schedule__option__classId__businessId",
        )
        .iterator()
    )
    # --- END OF NEW QUERY ---

    logger.info(
        f"Starting upcoming booking reminder task. Checking for bookings between {reminder_start_time} and {reminder_end_time}."
    )

    sent_count = 0
    for booking in upcoming_bookings:
        try:
            # This logic remains the same: localize the business time and convert to UTC for comparison.
            business_tz_str = (
                booking.schedule_instance.schedule.option.classId.businessId.business_timezone
            )
            business_tz = timezone.pytz.timezone(business_tz_str)

            naive_datetime = timezone.datetime.combine(
                booking.schedule_instance.date, booking.schedule_instance.time
            )
            schedule_datetime_aware = business_tz.localize(naive_datetime)

            # Convert the business's local time to UTC for a correct comparison
            schedule_datetime_utc = schedule_datetime_aware.astimezone(
                timezone.pytz.utc
            )

            # Compare the UTC-converted time with our UTC window
            if not (reminder_start_time <= schedule_datetime_utc < reminder_end_time):
                continue  # Skip if not in the precise 1-hour window

            cache_key = f"booking_reminder_sent_{booking.id}"
            if cache.get(cache_key):
                continue

            recipient = booking.user or booking.contact
            if not recipient:
                logger.warning(
                    f"Booking {booking.id} has no user or contact to send a reminder to. Skipping."
                )
                continue

            send_booking_reminder_email(user=recipient, booking=booking)
            sent_count += 1

            cache.set(cache_key, timeout=90000)  # 25 hours
            logger.info(
                f"Queued reminder email for booking {booking.id} to {recipient.email}."
            )

        except Exception as e:
            logger.error(
                f"Failed to process or send reminder for booking {booking.id}: {e}",
                exc_info=True,
            )

    logger.info(f"Task finished. Sent {sent_count} booking reminder emails.")
    return f"Completed sending {sent_count} booking reminders."
