# quickstart/tasks/booking_tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache

from ..models import Booking
from ..utils.email_utils import send_booking_reminder_email
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_upcoming_booking_reminders():
    """
    Sends reminder emails for confirmed bookings that are scheduled to start
    within the next 23 to 24 hours.

    This task should be scheduled to run hourly. It uses a cache to ensure
    that a reminder for any given booking is sent only once.
    """
    now = timezone.now()
    # Define the time window for reminders (e.g., classes starting in the next 23-24 hours)
    reminder_start_time = now + timedelta(hours=23)
    reminder_end_time = now + timedelta(hours=24)

    # Find all confirmed bookings within this upcoming time window
    upcoming_bookings = (
        Booking.objects.filter(
            status="confirmed",
            schedule_instance__date__gte=reminder_start_time.date(),
            schedule_instance__date__lte=reminder_end_time.date(),
        )
        .select_related(
            "user",
            "schedule_instance__schedule__option__classId__businessId",
        )
        .iterator()
    )  # Use iterator for memory efficiency if the volume is high

    logger.info(
        f"Starting upcoming booking reminder task. Checking for bookings between {reminder_start_time} and {reminder_end_time}."
    )

    sent_count = 0
    for booking in upcoming_bookings:
        # Combine the date and time from the schedule instance to create a datetime object
        # This requires fetching the business's timezone for accuracy.
        try:
            business_tz_str = (
                booking.schedule_instance.schedule.option.classId.businessId.business_timezone
            )
            business_tz = timezone.pytz.timezone(business_tz_str)
            naive_datetime = timezone.datetime.combine(
                booking.schedule_instance.date, booking.schedule_instance.time
            )
            schedule_datetime_aware = business_tz.localize(naive_datetime)

            # Check if this specific datetime falls within our target window
            if not (reminder_start_time <= schedule_datetime_aware < reminder_end_time):
                continue

            # Use a cache key to prevent sending duplicate reminders
            cache_key = f"booking_reminder_sent_{booking.id}"
            if cache.get(cache_key):
                # logger.debug(f"Reminder for booking {booking.id} already sent. Skipping.")
                continue

            # Send the email
            send_booking_reminder_email(user=booking.user, booking=booking)
            sent_count += 1

            # Set the cache key with a timeout slightly longer than the reminder window (e.g., 25 hours)
            # This prevents re-sending if the task re-runs or overlaps.
            cache.set(cache_key, True, timeout=90000)  # 25 hours in seconds
            logger.info(
                f"Queued reminder email for booking {booking.id} to {booking.user.email}."
            )

        except Exception as e:
            logger.error(
                f"Failed to process or send reminder for booking {booking.id}: {e}",
                exc_info=True,
            )

    logger.info(f"Task finished. Sent {sent_count} booking reminder emails.")
    return f"Completed sending {sent_count} booking reminders."
