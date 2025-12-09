from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import stripe
import logging
import pytz
import random

from quickstart.models import Booking, Payout

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
    Groups paid-out bookings by business and initiates Stripe transfers
    based on the allocated_net_payout stored on each booking.
    """
    logger.info("--- Starting Daily Payout Processing Task ---")

    # Find bookings that are finished and paid by customer, but not paid to business
    bookings_to_payout = (
        Booking.objects.filter(
            status__in=["completed", "forfeited"], 
            payment_status="paid",   
            payout_status="pending", 
        )
        .select_related("schedule_instance__schedule__option__classId__businessId")
    )

    if not bookings_to_payout.exists():
        logger.info("No bookings found requiring payout. Process finished.")
        return "No bookings to pay out."

    payouts_by_business = {}
    
    for booking in bookings_to_payout:
        business = booking.schedule_instance.schedule.option.classId.businessId
        
        # Skip if business not connected to Stripe
        if not business.stripe_account_id:
            continue
            
        # Initialize grouping structure
        payouts_by_business.setdefault(
            business.stripe_account_id,
            {
                "business_instance": business,
                "total_payout": Decimal("0.0"),
                "booking_ids": [],
            },
        )

        # Accumulate the 1/N share calculated at booking time
        if booking.allocated_net_payout > 0:
            payouts_by_business[business.stripe_account_id]["total_payout"] += booking.allocated_net_payout
            payouts_by_business[business.stripe_account_id]["booking_ids"].append(booking.id)
        else:
            # Handle $0 bookings (free or fully discounted)
            # We still mark them processed so they don't stay pending forever
            booking.payout_status = "processed"
            booking.save(update_fields=["payout_status"])

    logger.info(f"Found {len(payouts_by_business)} businesses to process payouts for.")

    successful_payouts = 0
    failed_payouts = 0

    for stripe_id, data in payouts_by_business.items():
        business = data["business_instance"]
        net_payout_amount = data["total_payout"]
        booking_ids = data["booking_ids"]

        # If total is 0 or too low (e.g., only free bookings were completed), skip transfer
        if net_payout_amount <= Decimal("0.50"):
            if booking_ids:
                # Just mark them processed if we aren't sending money
                Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")
            continue

        payout_record = None
        temp_transfer_id = f"temp_task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{business.businessId}_{random.randint(1000, 9999)}"

        try:
            with transaction.atomic():
                payout_amount_cents = int(net_payout_amount * 100)

                payout_record = Payout.objects.create(
                    business=business,
                    stripe_transfer_id=temp_transfer_id,
                    amount=net_payout_amount,
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

                transfer = stripe.Transfer.create(
                    amount=payout_amount_cents,
                    currency=business.currency.lower(),
                    destination=stripe_id,
                    description=f"ClassEasily Payout",
                    metadata={
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "payout_record_id": str(payout_record.id),
                    },
                )

                arrival_date = (
                    datetime.fromtimestamp(transfer.arrival_date, tz=pytz.utc).date()
                    if transfer.arrival_date
                    else timezone.now().date() + timedelta(days=3)
                )

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

                bookings = Booking.objects.filter(id__in=booking_ids)
                payout_record.bookings.set(bookings)
                bookings.update(payout_status="processed")

                successful_payouts += 1
                logger.info(
                    f"SUCCESS: Created Stripe Transfer {transfer.id} for Business {business.businessId}. Amount: ${net_payout_amount}"
                )

        except stripe.error.StripeError as stripe_error:
            failed_payouts += 1
            if payout_record:
                payout_record.status = "failed"
                payout_record.save(update_fields=["status"])
                logger.error(f"STRIPE ERROR for Business {business.businessId}: {stripe_error}")

        except Exception as general_error:
            failed_payouts += 1
            if payout_record:
                payout_record.status = "failed"
                payout_record.save(update_fields=["status"])
            logger.error(f"GENERAL ERROR for Business {business.businessId}: {general_error}", exc_info=True)

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
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                
                # Basic validation checks
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

                # --- 1. CORRECT CALCULATION LOGIC ---
                # Use the booking's specific allocated value, not the whole payment
                base_value = locked_booking.amount_paid
                
                refund_percentage = (
                    Decimal(locked_booking.cancellation_refund_percentage) / 100
                )
                amount_to_refund = (base_value * refund_percentage).quantize(Decimal("0.01"))

                # Safety cap against Stripe balance
                if amount_to_refund > payment.available_refund_amount:
                    amount_to_refund = payment.available_refund_amount

                # --- 2. OPTION B LOGIC: ZERO REFUND HANDLING ---
                if amount_to_refund < Decimal("0.50"):
                    logger.info(
                        f"Booking {locked_booking.id}: Refund amount is ${amount_to_refund}. "
                        "Marking as 'forfeited' (Business keeps funds)."
                    )
                    
                    # CRITICAL: Switch status so Payout Task picks it up
                    locked_booking.status = "forfeited" 
                    
                    # Reset payment status so Payout Task accepts it
                    locked_booking.payment_status = "paid" 
                    
                    # Log the reason
                    locked_booking.cancellation_reason = (
                        f"Cancelled (Non-refundable). Refund calculated: ${amount_to_refund}"
                    )
                    
                    locked_booking.save(update_fields=[
                        "status", 
                        "payment_status", 
                        "cancellation_reason"
                    ])
                    
                    successful_refunds += 1
                    continue

                # --- 3. STANDARD REFUND (If > $0.50) ---
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