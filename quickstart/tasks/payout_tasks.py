from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import stripe
import logging
import pytz
import random

from quickstart.models import Booking, Payout, BusinessInfo

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


@shared_task
def update_completed_booking_status():
    """
    Marks past, confirmed bookings as 'completed' so they are eligible for payout.
    """
    yesterday = timezone.now().date() - timedelta(days=1)
    instances_to_complete = Q(schedule_instance__date__lte=yesterday)
    bookings_to_update = Booking.objects.filter(
        instances_to_complete, status="confirmed"
    )
    updated_count = bookings_to_update.update(status="completed")
    if updated_count > 0:
        logger.info(
            f"Successfully marked {updated_count} past bookings as 'completed'."
        )
    return f"Completed status update. {updated_count} bookings marked as 'completed'."


@shared_task
def process_daily_payouts():
    """
    Groups paid-out bookings by business and initiates Stripe transfers.
    Uses database locking to prevent race conditions (double payouts).
    """
    logger.info("--- Starting Daily Payout Processing Task ---")

    # 1. Identify which businesses have pending payouts
    # We do a lightweight query first to get the IDs, avoiding a massive lock.
    business_ids_with_pending = Booking.objects.filter(
        status__in=["completed", "forfeited"],
        payment_status="paid",
        payout_status="pending"
    ).values_list('schedule_instance__schedule__option__classId__businessId', flat=True).distinct()

    if not business_ids_with_pending:
        logger.info("No bookings found requiring payout. Process finished.")
        return "No bookings to pay out."
    
    logger.info(f"Found {len(business_ids_with_pending)} businesses to process.")

    successful_payouts = 0
    failed_payouts = 0

    # 2. Process each business individually with a transaction lock
    for business_id in business_ids_with_pending:
        try:
            with transaction.atomic():
                # Fetch Business & Stripe Account
                business = BusinessInfo.objects.get(pk=business_id)
                if not business.stripe_account_id:
                    continue

                # LOCKING: Select relevant bookings for update to prevent double-processing
                bookings_to_process = Booking.objects.select_for_update(skip_locked=True).filter(
                    schedule_instance__schedule__option__classId__businessId=business,
                    status__in=["completed", "forfeited"],
                    payment_status="paid",
                    payout_status="pending"
                )
                
                # Check if we still have bookings after locking
                if not bookings_to_process.exists():
                    continue

                # Calculate total payout amount
                total_payout = Decimal("0.00")
                booking_ids = []
                
                for booking in bookings_to_process:
                    if booking.allocated_net_payout > 0:
                        total_payout += booking.allocated_net_payout
                        booking_ids.append(booking.id)
                    else:
                        # Mark $0/free bookings as processed immediately
                        booking.payout_status = "processed"
                        booking.save(update_fields=["payout_status"])

                # If no funds to transfer (only free classes), skip Stripe
                if total_payout <= Decimal("0.50"):
                    if booking_ids:
                        Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")
                    continue

                # Create Payout Record
                temp_transfer_id = f"temp_task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{business.businessId}_{random.randint(1000, 9999)}"
                payout_amount_cents = int(total_payout * 100)

                payout_record = Payout.objects.create(
                    business=business,
                    stripe_transfer_id=temp_transfer_id,
                    amount=total_payout,
                    currency=business.currency.upper(),
                    arrival_date=timezone.now().date() + timedelta(days=3),
                    status="pending",
                    metadata={
                        "source": "daily_payout_task",
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "temp_id": True,
                    },
                )

                # Execute Stripe Transfer
                transfer = stripe.Transfer.create(
                    amount=payout_amount_cents,
                    currency=business.currency.lower(),
                    destination=business.stripe_account_id,
                    description=f"ClassEasily Payout",
                    metadata={
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "payout_record_id": str(payout_record.id),
                    },
                    idempotency_key=temp_transfer_id
                )

                arrival_date = timezone.now().date()

                updated_metadata = transfer.metadata.copy()
                updated_metadata.pop("temp_id", None)

                payout_record.stripe_transfer_id = transfer.id
                payout_record.arrival_date = arrival_date
                payout_record.status = "paid"
                payout_record.metadata = updated_metadata
                payout_record.save(
                    update_fields=[
                        "stripe_transfer_id",
                        "arrival_date",
                        "status",
                        "metadata",
                    ]
                )

                # Bulk update bookings to processed
                Booking.objects.filter(id__in=booking_ids).update(payout_status="processed", payouts=payout_record)
                
                successful_payouts += 1
                logger.info(f"SUCCESS: Payout {transfer.id} for Business {business.businessId}: ${total_payout}")

        except stripe.StripeError as e:
            failed_payouts += 1
            logger.error(f"Stripe Error for Business {business_id}: {e}")
            # Note: Transaction rollback will occur automatically, keeping bookings as 'pending'
            
        except Exception as e:
            failed_payouts += 1
            logger.error(f"Error processing payout for Business {business_id}: {e}", exc_info=True)

    return f"Payout process finished. Successful: {successful_payouts}. Failed: {failed_payouts}."


@shared_task
def process_daily_refunds():
    logger.info("--- Starting Daily Refund Processing Task ---")

    bookings_to_refund = Booking.objects.filter(payment_status="refund_pending")

    if not bookings_to_refund.exists():
        return "No refunds to process."

    successful_refunds, failed_refunds = 0, 0

    for booking in bookings_to_refund:
        try:
            with transaction.atomic():
                # Lock the specific booking row
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                
                # Double check status inside the lock
                if locked_booking.payment_status != "refund_pending":
                    continue
                
                payment = locked_booking.payments.filter(
                    status__in=["succeeded", "partially_refunded"]
                ).first()

                if not payment:
                    locked_booking.payment_status = "refund_failed"
                    locked_booking.save(update_fields=["payment_status"])
                    failed_refunds += 1
                    continue

                # Calculate Refund
                base_value = locked_booking.amount_paid
                refund_percentage = (Decimal(locked_booking.cancellation_refund_percentage) / 100)
                amount_to_refund = (base_value * refund_percentage).quantize(Decimal("0.01"))

                # Safety cap against available funds
                if amount_to_refund > payment.available_refund_amount:
                    amount_to_refund = payment.available_refund_amount

                # Option B: Zero/Low Refund Handling
                if amount_to_refund < Decimal("0.50"):
                    logger.info(
                        f"Booking {locked_booking.id}: Refund ${amount_to_refund} too low. Forfeiting to business."
                    )
                    # Switch status so Payout Task picks it up
                    locked_booking.status = "forfeited" 
                    locked_booking.payment_status = "paid" 
                    locked_booking.cancellation_reason = (
                        f"Cancelled (Non-refundable). Refund calculated: ${amount_to_refund}"
                    )
                    locked_booking.save(update_fields=["status", "payment_status", "cancellation_reason"])
                    successful_refunds += 1
                    continue

                # Standard Refund
                stripe_refund = stripe.Refund.create(
                    payment_intent=payment.stripe_payment_intent_id,
                    amount=int(amount_to_refund * 100),
                    reason="requested_by_customer",
                )

                payment.refunded_amount += Decimal(stripe_refund.amount) / 100
                if payment.available_refund_amount <= Decimal("0.00"):
                    payment.status = "refunded"
                else:
                    payment.status = "partially_refunded"
                payment.save(update_fields=["refunded_amount", "status"])

                locked_booking.payment_status = "refunded"
                locked_booking.save(update_fields=["payment_status"])

                successful_refunds += 1

        except Exception as e:
            logger.error(f"Error refunding Booking {booking.id}: {e}", exc_info=True)
            failed_refunds += 1

    return f"Refunds processed. Success: {successful_refunds}, Failed: {failed_refunds}"