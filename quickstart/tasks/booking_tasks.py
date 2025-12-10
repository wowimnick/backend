import pytz
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
import stripe
from django.core.cache import cache
from quickstart.models import Booking, CourseEnrollment, Payment
from django.db import transaction
from django.conf import settings
from quickstart.utils.email_utils import send_booking_reminder_email
import logging

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

@shared_task  
def release_expired_spots():
    """
    Hard deletes bookings that have been pending for more than 15 minutes.
    Releases the spot back to the schedule and cleans up the database.
    """
    # 1. Define timeout (15 minutes allowed for checkout)
    timeout_threshold = timezone.now() - timedelta(minutes=15)

    # 2. Find stale bookings
    stale_bookings = Booking.objects.filter(
        status='pending',
        booking_date__lt=timeout_threshold
    )

    if not stale_bookings.exists():
        return 

    count = stale_bookings.count()
    logger.info(f"Found {count} stale bookings to release/delete.")

    for booking in stale_bookings:
        try:
            # Check if booking still exists (it might have been deleted as part of a group in a previous iteration)
            if not Booking.objects.filter(pk=booking.pk).exists():
                continue

            with transaction.atomic():
                # 3. Cancel Stripe Intent (Release hold on card/funds)
                payment = Payment.objects.filter(booking=booking).first()
                if payment and payment.stripe_payment_intent_id and not payment.stripe_payment_intent_id.startswith('temp'):
                    try:
                        stripe.PaymentIntent.cancel(payment.stripe_payment_intent_id)
                        logger.info(f"Cancelled Stripe Intent {payment.stripe_payment_intent_id}")
                    except stripe.error.StripeError as e:
                        logger.warning(f"Could not cancel intent {payment.stripe_payment_intent_id}: {e}")

                # 4. Hard Delete Logic
                if booking.booking_group_id:
                    # Full Course: Clean up the Enrollment and ALL sibling bookings
                    group_id = booking.booking_group_id
                    CourseEnrollment.objects.filter(booking_group_id=group_id).delete()
                    deleted_count, _ = Booking.objects.filter(booking_group_id=group_id).delete()
                    logger.info(f"Hard deleted expired course group {group_id} ({deleted_count} bookings).")
                else:
                    # Single Session: Delete just this booking
                    booking_id = booking.id
                    booking.delete()
                    logger.info(f"Hard deleted expired booking {booking_id}.")

        except Exception as e:
            logger.error(f"Error cleaning up booking {booking.id}: {e}", exc_info=True)


@shared_task
def send_upcoming_booking_reminders():
    """
    Sends reminder emails for confirmed bookings scheduled to start
    within the next 22 to 24 hours.
    
    ZERO SPAM GUARANTEE:
    Uses atomic cache.add() to lock the booking ID before sending.
    """
    now = timezone.now()
    reminder_start_time = now + timedelta(hours=22)
    reminder_end_time = now + timedelta(hours=24)

    possible_dates = {reminder_start_time.date(), reminder_end_time.date()}

    upcoming_bookings = (
        Booking.objects.filter(
            status="confirmed",
            schedule_instance__date__in=possible_dates,
            schedule_instance__schedule__option__classId__businessId__reminderNotification=True
        )
        .select_related(
            "user",
            "contact",
            "schedule_instance__schedule__option__classId__businessId",
        )
        .iterator()
    )

    logger.info(f"Starting reminder task. Window: {reminder_start_time} to {reminder_end_time}.")

    sent_count = 0
    for booking in upcoming_bookings:
        # ATOMIC LOCKING: Define key
        cache_key = f"booking_reminder_sent_{booking.id}"
        
        # Try to acquire lock IMMEDIATELY. 
        # If cache.add returns False, the key exists (sent or in progress) -> SKIP.
        # Lock duration: 30 hours (covers the entire 24h window + buffer)
        if not cache.add(cache_key, True, timeout=108000):
            continue

        try:
            # Timezone Logic
            business_tz_str = booking.schedule_instance.schedule.option.classId.businessId.business_timezone
            business_tz = timezone.pytz.timezone(business_tz_str)

            naive_datetime = timezone.datetime.combine(
                booking.schedule_instance.date, booking.schedule_instance.time
            )
            schedule_datetime_aware = business_tz.localize(naive_datetime)
            schedule_datetime_utc = schedule_datetime_aware.astimezone(timezone.pytz.utc)

            # Precision Check
            if not (reminder_start_time <= schedule_datetime_utc < reminder_end_time):
                # IMPORTANT: If we skipped because of time, we must RELEASE the lock
                # so it can be picked up in the next hour if valid.
                cache.delete(cache_key)
                continue  

            recipient = booking.user or booking.contact
            if not recipient:
                logger.warning(f"Booking {booking.id} has no recipient. Skipping.")
                continue

            send_booking_reminder_email(user=recipient, booking=booking)
            sent_count += 1
            logger.info(f"Queued reminder email for booking {booking.id}.")

        except Exception as e:
            # If sending fails, we do NOT release the lock. 
            # This prevents a "Retry Loop" from spamming the user if the error 
            # was a network timeout where the email actually did go out.
            logger.error(f"Failed to send reminder for booking {booking.id}: {e}", exc_info=True)

    return f"Completed sending {sent_count} booking reminders."