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
    logger.info("=" * 80)
    logger.info("TASK START: update_completed_booking_status")
    logger.info("=" * 80)
    
    yesterday = timezone.now().date() - timedelta(days=1)
    logger.info(f"Looking for bookings on or before: {yesterday}")
    
    instances_to_complete = Q(schedule_instance__date__lte=yesterday)
    bookings_to_update = Booking.objects.filter(
        instances_to_complete, status="confirmed"
    )
    
    count_to_update = bookings_to_update.count()
    logger.info(f"Found {count_to_update} confirmed bookings to mark as completed")
    
    updated_count = bookings_to_update.update(status="completed")
    
    logger.info(f"Successfully marked {updated_count} past bookings as 'completed'")
    logger.info("=" * 80)
    logger.info("TASK END: update_completed_booking_status")
    logger.info("=" * 80)
    
    return f"Completed status update. {updated_count} bookings marked as 'completed'."

@shared_task
def process_daily_payouts():
    """
    Groups paid-out bookings by business and initiates Stripe transfers.
    Uses database locking to prevent race conditions (double payouts).
    """
    logger.info("=" * 80)
    logger.info("TASK START: process_daily_payouts")
    logger.info(f"Task triggered at: {timezone.now()}")
    logger.info("=" * 80)

    # 1. Identify which businesses have pending payouts
    # We do a lightweight query first to get the IDs, avoiding a massive lock.
    business_ids_with_pending = Booking.objects.filter(
        status__in=["completed", "forfeited"],
        payment_status="paid",
        payout_status="pending"
    ).values_list('schedule_instance__schedule__option__classId__businessId', flat=True).distinct()

    if not business_ids_with_pending:
        logger.info("No bookings found requiring payout. Process finished.")
        logger.info("=" * 80)
        logger.info("TASK END: process_daily_payouts (No work to do)")
        logger.info("=" * 80)
        return "No bookings to pay out."
    
    logger.info(f"Found {len(business_ids_with_pending)} businesses with pending payouts to process")
    logger.info(f"Business IDs: {list(business_ids_with_pending)}")

    successful_payouts = 0
    failed_payouts = 0

    # 2. Process each business individually with a transaction lock
    for idx, business_id in enumerate(business_ids_with_pending, 1):
        logger.info(f"--- Processing Business {idx}/{len(business_ids_with_pending)}: ID={business_id} ---")
        try:
            with transaction.atomic():
                # Fetch Business & Stripe Account
                business = BusinessInfo.objects.get(pk=business_id)
                logger.info(f"Business Name: {business.businessName}")
                
                if not business.stripe_account_id:
                    logger.warning(f"Business {business_id} has no Stripe account. Skipping.")
                    continue

                logger.info(f"Stripe Account ID: {business.stripe_account_id}")

                # LOCKING: Select relevant bookings for update to prevent double-processing
                bookings_to_process = Booking.objects.select_for_update(skip_locked=True).filter(
                    schedule_instance__schedule__option__classId__businessId=business,
                    status__in=["completed", "forfeited"],
                    payment_status="paid",
                    payout_status="pending"
                )
                
                booking_count_locked = bookings_to_process.count()
                logger.info(f"Acquired lock on {booking_count_locked} bookings")
                
                # Check if we still have bookings after locking
                if not bookings_to_process.exists():
                    logger.info(f"No bookings remaining after lock acquisition (another worker processing?). Skipping.")
                    continue

                # Calculate total payout amount
                total_payout = Decimal("0.00")
                booking_ids = []
                zero_dollar_bookings = []
                
                for booking in bookings_to_process:
                    if booking.allocated_net_payout > 0:
                        total_payout += booking.allocated_net_payout
                        booking_ids.append(booking.id)
                    else:
                        # Mark $0/free bookings as processed immediately
                        zero_dollar_bookings.append(booking.id)

                logger.info(f"Total payout amount calculated: ${total_payout}")
                logger.info(f"Bookings to pay out: {len(booking_ids)}")
                logger.info(f"Zero-dollar bookings to mark processed: {len(zero_dollar_bookings)}")

                # Mark zero-dollar bookings as processed
                if zero_dollar_bookings:
                    Booking.objects.filter(id__in=zero_dollar_bookings).update(payout_status="processed")
                    logger.info(f"Marked {len(zero_dollar_bookings)} zero-dollar bookings as processed")

                if total_payout < Decimal("0.50"): # Stripe minimum varies, 0.50 is safe for CAD/USD
                    # Do NOT mark as processed. Just skip. 
                    # They will be picked up in the next run and aggregated with new bookings.
                    logger.info(f"Skipping payout for Business {business.businessId}: Amount ${total_payout} below minimum threshold.")
                    continue

                # Create Payout Record
                temp_transfer_id = f"temp_task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{business.businessId}_{random.randint(1000, 9999)}"
                payout_amount_cents = int(total_payout * 100)

                logger.info(f"Creating payout record with temp ID: {temp_transfer_id}")

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

                logger.info(f"Payout record created: ID={payout_record.id}")
                logger.info(f"Initiating Stripe Transfer: Amount={payout_amount_cents} cents, Currency={business.currency.lower()}")

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

                logger.info(f"Stripe Transfer successful: ID={transfer.id}")

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

                logger.info(f"Updated payout record with Stripe Transfer ID: {transfer.id}")

                # 1. Bulk update the status fields
                Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")
                logger.info(f"Marked {len(booking_ids)} bookings as payout_status='processed'")
                
                # 2. Bulk link the bookings to the payout record
                # Note: 'bookings' is the related_name defined in the Booking model for the payouts field
                payout_record.bookings.add(*booking_ids)
                logger.info(f"Linked {len(booking_ids)} bookings to payout record")
                
                successful_payouts += 1
                logger.info(f"✓ SUCCESS: Payout {transfer.id} for Business {business.businessId}: ${total_payout}")

        except stripe.StripeError as e:
            failed_payouts += 1
            logger.error(f"✗ STRIPE ERROR for Business {business_id}: {e}")
            # Note: Transaction rollback will occur automatically, keeping bookings as 'pending'
            
        except Exception as e:
            failed_payouts += 1
            logger.error(f"✗ ERROR processing payout for Business {business_id}: {e}", exc_info=True)

    logger.info("=" * 80)
    logger.info(f"TASK END: process_daily_payouts")
    logger.info(f"Summary: Successful={successful_payouts}, Failed={failed_payouts}")
    logger.info("=" * 80)
    
    return f"Payout process finished. Successful: {successful_payouts}. Failed: {failed_payouts}."

@shared_task
def process_daily_refunds():
    logger.info("=" * 80)
    logger.info("TASK START: process_daily_refunds")
    logger.info(f"Task triggered at: {timezone.now()}")
    logger.info("=" * 80)

    bookings_to_refund = Booking.objects.filter(payment_status="refund_pending")

    if not bookings_to_refund.exists():
        logger.info("No refunds to process.")
        logger.info("=" * 80)
        logger.info("TASK END: process_daily_refunds (No work to do)")
        logger.info("=" * 80)
        return "No refunds to process."

    logger.info(f"Found {bookings_to_refund.count()} bookings requiring refund")

    successful_refunds, failed_refunds = 0, 0

    for idx, booking in enumerate(bookings_to_refund, 1):
        logger.info(f"--- Processing Refund {idx}/{bookings_to_refund.count()}: Booking ID={booking.id} ---")
        try:
            with transaction.atomic():
                # Lock the specific booking row
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                
                # Double check status inside the lock
                if locked_booking.payment_status != "refund_pending":
                    logger.info(f"Booking {booking.id} status changed (now {locked_booking.payment_status}). Skipping.")
                    continue
                
                payment = locked_booking.payments.filter(
                    status__in=["succeeded", "partially_refunded"]
                ).first()

                if not payment:
                    logger.warning(f"No valid payment found for Booking {booking.id}. Marking as refund_failed.")
                    locked_booking.payment_status = "refund_failed"
                    locked_booking.save(update_fields=["payment_status"])
                    failed_refunds += 1
                    continue

                logger.info(f"Payment found: ID={payment.id}, Available to refund: ${payment.available_refund_amount}")

                # Calculate Refund
                base_value = locked_booking.amount_paid
                refund_percentage = (Decimal(locked_booking.cancellation_refund_percentage) / 100)
                amount_to_refund = (base_value * refund_percentage).quantize(Decimal("0.01"))

                logger.info(f"Refund calculation: Base=${base_value}, Percentage={locked_booking.cancellation_refund_percentage}%, Amount=${amount_to_refund}")

                # Safety cap against available funds
                if amount_to_refund > payment.available_refund_amount:
                    logger.warning(f"Refund amount ${amount_to_refund} exceeds available ${payment.available_refund_amount}. Capping.")
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
                    logger.info(f"✓ Booking {locked_booking.id} forfeited to business (sub-minimum refund)")
                    continue

                # Standard Refund
                logger.info(f"Initiating Stripe refund: Amount=${amount_to_refund}, Payment Intent={payment.stripe_payment_intent_id}")
                
                stripe_refund = stripe.Refund.create(
                    payment_intent=payment.stripe_payment_intent_id,
                    amount=int(amount_to_refund * 100),
                    reason="requested_by_customer",
                )

                logger.info(f"Stripe refund successful: Refund ID={stripe_refund.id}")

                payment.refunded_amount += Decimal(stripe_refund.amount) / 100
                if payment.available_refund_amount <= Decimal("0.00"):
                    payment.status = "refunded"
                else:
                    payment.status = "partially_refunded"
                payment.save(update_fields=["refunded_amount", "status"])

                logger.info(f"Updated Payment {payment.id}: Refunded=${payment.refunded_amount}, Status={payment.status}")

                locked_booking.payment_status = "refunded"
                locked_booking.save(update_fields=["payment_status"])

                successful_refunds += 1
                logger.info(f"✓ SUCCESS: Booking {locked_booking.id} refunded ${amount_to_refund}")

        except Exception as e:
            logger.error(f"✗ ERROR refunding Booking {booking.id}: {e}", exc_info=True)
            failed_refunds += 1

    logger.info("=" * 80)
    logger.info(f"TASK END: process_daily_refunds")
    logger.info(f"Summary: Successful={successful_refunds}, Failed={failed_refunds}")
    logger.info("=" * 80)

    return f"Refunds processed. Success: {successful_refunds}, Failed: {failed_refunds}"