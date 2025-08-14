# quickstart/tasks/user_tasks.py
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from ..models import Booking, Reviews
from ..utils.email_utils import send_request_for_review_email
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_pending_review_requests():
    """
    Sends review request emails for bookings that were completed
    between 24 and 48 hours ago and have not yet been reviewed.
    """
    # Target a window to catch bookings completed yesterday.
    start_time = timezone.now() - timedelta(hours=48)
    end_time = timezone.now() - timedelta(hours=24)

    # Find bookings that are 'completed', have a schedule instance date within the target window,
    # and do NOT have an associated review yet.
    bookings_needing_review = (
        Booking.objects.filter(
            status="completed",
            schedule_instance__date__gte=start_time.date(),
            schedule_instance__date__lt=end_time.date() + timedelta(days=1),
            review__isnull=True,  # Check the related_name from Reviews model's ForeignKey
        )
        .select_related("user", "schedule_instance__schedule__option__classId")
        .distinct()
    )

    count = 0
    for booking in bookings_needing_review:
        # Final check to be absolutely sure no review was created in a race condition.
        if not Reviews.objects.filter(booking=booking).exists():
            try:
                send_request_for_review_email(user=booking.user, booking=booking)
                count += 1
            except Exception as e:
                logger.error(
                    f"Failed to send review request for booking {booking.id}: {e}",
                    exc_info=True,
                )

    logger.info(f"Queued {count} review request emails.")
    return f"Processed and queued {count} review request emails."
