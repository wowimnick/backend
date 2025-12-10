from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.core.cache import cache
from quickstart.models import Booking, Reviews
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_pending_review_requests():
    """
    Sends review request emails for bookings completed 24-48h ago.
    
    ZERO SPAM GUARANTEE:
    Uses atomic cache.add() to lock the booking ID.
    """
    from quickstart.utils.email_utils import send_request_for_review_email

    start_time = timezone.now() - timedelta(hours=48)
    end_time = timezone.now() - timedelta(hours=24)

    bookings_needing_review = (
        Booking.objects.filter(
            status="completed",
            schedule_instance__date__gte=start_time.date(),
            schedule_instance__date__lt=end_time.date() + timedelta(days=1),
            review__isnull=True,
        )
        .select_related("user", "schedule_instance__schedule__option__classId")
        .distinct()
    )

    count = 0
    skipped = 0
    
    for booking in bookings_needing_review:
        cache_key = f"review_request_sent_lock_{booking.id}"
        
        # ATOMIC LOCK: 7 Days timeout.
        # If this returns False, we skip immediately. No race condition possible.
        if not cache.add(cache_key, True, timeout=604800):
            skipped += 1
            continue

        # Database Check (Double protection)
        if Reviews.objects.filter(booking=booking).exists():
            continue

        try:
            send_request_for_review_email(user=booking.user, booking=booking)
            count += 1
        except Exception as e:
            logger.error(
                f"Failed to send review request for booking {booking.id}: {e}",
                exc_info=True,
            )
            # Do NOT delete cache key on error. 
            # Prioritize "Zero Spam" over "Retry Success".

    logger.info(f"Review Requests: Queued {count} emails. Skipped {skipped}.")
    return f"Processed review requests. Queued: {count}."