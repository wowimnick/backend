from celery import shared_task
from django.utils import timezone
from quickstart.models import GiftCard
from quickstart.utils.email_utils import send_gift_card_email
import logging

logger = logging.getLogger(__name__)


@shared_task(name="quickstart.tasks.giftcard_tasks.process_scheduled_gift_cards")
def process_scheduled_gift_cards():
    """
    Checks for gift cards that are scheduled for today (or past dates)
    that haven't been sent yet.
    """
    today = timezone.now().date()

    # Filter: Scheduled + Active + Not Sent Yet + Date is Today or Past
    due_gift_cards = GiftCard.objects.filter(
        is_scheduled=True, is_active=True, email_sent=False, scheduled_date__lte=today
    )

    count = due_gift_cards.count()
    if count == 0:
        return "No scheduled gift cards to send."

    logger.info(f"Found {count} scheduled gift cards due for delivery.")

    sent_count = 0
    for gc in due_gift_cards:
        try:
            send_gift_card_email(gc)
            sent_count += 1
        except Exception as e:
            logger.error(
                f"Failed to process scheduled gift card {gc.code}: {e}", exc_info=True
            )

    return f"Successfully processed {sent_count} scheduled gift cards."
