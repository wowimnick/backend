from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import stripe
import logging
import pytz
import random

from quickstart.models import Booking, Payout, BusinessInfo, Payment
from quickstart.utils.notification_utils import create_notification_for_recipients
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict

logger = logging.getLogger(__name__)

# Stripe standard processing fee (approximate): 2.9% + fixed fee per charge. Deducted from business payout.
STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_FIXED = Decimal("0.30")


def _estimate_stripe_processing_fee(charge_amount):
    """Estimate Stripe processing fee for a charge (2.9% + $0.30). charge_amount in dollars."""
    if charge_amount <= 0:
        return Decimal("0.00")
    fee = (charge_amount * STRIPE_FEE_PERCENT + STRIPE_FEE_FIXED).quantize(Decimal("0.01"))
    return fee


def _maybe_send_payout_connect_reminder(business):
    """
    If the business has pending payout bookings and we haven't sent the
    "connect Stripe" reminder in the last 3 days, send it and update the cooldown.
    """
    from quickstart.utils.email_utils import send_payout_connect_required_email

    pending_bookings = (
        Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status__in=["completed", "forfeited"],
            payment_status="paid",
            payout_status="pending",
        )
        .prefetch_related("payments")
    )
    if not pending_bookings.exists():
        return

    total_payout = Decimal("0.00")
    booking_count = 0
    for booking in pending_bookings:
        allocated = booking.allocated_net_payout or Decimal("0.00")
        if allocated <= 0:
            continue
        payment = next(
            (p for p in booking.payments.all() if p.status == "succeeded"),
            None,
        )
        stripe_fee = Decimal("0.00")
        if payment and payment.amount:
            stripe_fee = _estimate_stripe_processing_fee(payment.amount)
        net_after_stripe = (allocated - stripe_fee).quantize(Decimal("0.01"))
        if net_after_stripe > 0:
            total_payout += net_after_stripe
            booking_count += 1

    if booking_count == 0:
        return

    last_sent = getattr(business, "last_payout_connect_reminder_sent", None)
    if last_sent and (timezone.now() - last_sent).days < 3:
        logger.info(
            f"Payout connect reminder cooldown: Business {business.businessId} "
            f"last sent {last_sent}. Skipping."
        )
        return

    owner = business.owner
    if not owner or not owner.email:
        logger.warning(
            f"Business {business.businessId} has no owner email for payout connect reminder."
        )
        return

    send_payout_connect_required_email(
        business_user=owner,
        pending_amount=total_payout,
        booking_count=booking_count,
        currency=business.currency or "CAD",
    )
    BusinessInfo.objects.filter(pk=business.businessId).update(
        last_payout_connect_reminder_sent=timezone.now()
    )
    logger.info(
        f"Sent payout connect required email to {owner.email} for Business {business.businessId} "
        f"(pending_amount=${total_payout}, booking_count={booking_count})"
    )


def _on_cancellation_refund_failed(booking, reason):
    """
    When a cancellation refund could not be completed: log critical for internal alert
    and optionally email the guest that support will follow up.
    """
    logger.critical(
        "REFUND_FAILED: Booking %s - Cancellation refund could not be completed. Reason: %s. Manual follow-up required.",
        booking.id,
        reason,
    )
    try:
        from quickstart.utils.email_utils import send_refund_failed_guest_email
        send_refund_failed_guest_email(booking, reason)
    except Exception as e:
        logger.warning("Could not send refund failed email to guest for booking %s: %s", booking.id, e)


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

    try:
        per_business = (
            bookings_to_update.values(
                "schedule_instance__schedule__option__classId__businessId"
            )
            .annotate(n=Count("id"))
            .order_by()
        )
        for row in per_business:
            bid = row["schedule_instance__schedule__option__classId__businessId"]
            n = row["n"]
            if not bid or n < 1:
                continue
            try:
                biz = BusinessInfo.objects.get(pk=bid)
            except BusinessInfo.DoesNotExist:
                continue
            done_msg = (
                f"You have {n} completed class(es) ready for payout."
                if n != 1
                else "You have 1 completed class ready for payout."
            )
            create_notification_for_recipients(
                biz,
                "booking_completed",
                done_msg,
                "CheckCircle",
                "#22c55e",
                "/business/dashboard?tab=payouts",
            )
    except Exception as e:
        logger.warning(
            "booking_completed in-app notifications skipped: %s", e, exc_info=True
        )

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
                    # Compute pending amount/count and send "connect Stripe" reminder at most once every 3 days
                    _maybe_send_payout_connect_reminder(business)
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

                # Prefetch payments for Stripe fee estimation
                bookings_to_process = bookings_to_process.prefetch_related("payments")

                # Calculate total payout amount (Stripe processing fees deducted from business payout)
                total_payout = Decimal("0.00")
                total_stripe_fees = Decimal("0.00")
                booking_ids = []
                zero_dollar_bookings = []

                for booking in bookings_to_process:
                    allocated = booking.allocated_net_payout or Decimal("0.00")
                    if allocated <= 0:
                        zero_dollar_bookings.append(booking.id)
                        continue
                    # Deduct estimated Stripe processing fee from this booking's payout
                    payment = next(
                        (p for p in booking.payments.all() if p.status == "succeeded"),
                        None,
                    )
                    stripe_fee = Decimal("0.00")
                    if payment and payment.amount:
                        stripe_fee = _estimate_stripe_processing_fee(payment.amount)
                    total_stripe_fees += stripe_fee
                    net_after_stripe = (allocated - stripe_fee).quantize(Decimal("0.01"))
                    if net_after_stripe > 0:
                        total_payout += net_after_stripe
                        booking_ids.append(booking.id)
                    else:
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
                        "stripe_fees_deducted": str(total_stripe_fees),
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

                base_meta = payout_record.metadata
                if not isinstance(base_meta, dict):
                    base_meta = {}
                updated_metadata = dict(base_meta)
                updated_metadata.update(
                    stripe_metadata_to_dict(getattr(transfer, "metadata", None))
                )
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
            try:
                business = BusinessInfo.objects.filter(pk=business_id).first()
                from quickstart.utils.email_utils import send_super_admin_payout_failed_email

                send_super_admin_payout_failed_email(
                    business_id, str(e), business=business, stripe_error=True
                )
            except Exception as email_err:
                logger.warning(
                    "Could not send Super Admin payout failure email: %s", email_err
                )
            # Note: Transaction rollback will occur automatically, keeping bookings as 'pending'

        except Exception as e:
            failed_payouts += 1
            logger.error(f"✗ ERROR processing payout for Business {business_id}: {e}", exc_info=True)
            try:
                business = BusinessInfo.objects.filter(pk=business_id).first()
                from quickstart.utils.email_utils import send_super_admin_payout_failed_email

                send_super_admin_payout_failed_email(
                    business_id, str(e), business=business, stripe_error=False
                )
            except Exception as email_err:
                logger.warning(
                    "Could not send Super Admin payout failure email: %s", email_err
                )

    logger.info("=" * 80)
    logger.info(f"TASK END: process_daily_payouts")
    logger.info(f"Summary: Successful={successful_payouts}, Failed={failed_payouts}")
    logger.info("=" * 80)
    
    return f"Payout process finished. Successful: {successful_payouts}. Failed: {failed_payouts}."

@shared_task
def process_daily_refunds():
    logger.info("=" * 80)
    logger.info("TASK START: process_daily_refunds")
    logger.info("=" * 80)

    bookings_to_refund = Booking.objects.filter(payment_status="refund_pending")

    if not bookings_to_refund.exists():
        logger.info("No refunds to process.")
        return "No refunds to process."

    successful_refunds, failed_refunds = 0, 0

    for idx, booking in enumerate(bookings_to_refund, 1):
        try:
            with transaction.atomic():
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                
                if locked_booking.payment_status != "refund_pending":
                    continue

                # 1. Calculate Total Refund Amount based on Policy
                base_value = locked_booking.amount_paid
                refund_percentage = (Decimal(locked_booking.cancellation_refund_percentage) / 100)
                total_refund_needed = (base_value * refund_percentage).quantize(Decimal("0.01"))
                
                remaining_refund_needed = total_refund_needed
                logger.info(f"Booking {booking.id}: Total refund calculated: ${total_refund_needed}")

                # 2. Handle Gift Card Refunds FIRST
                # Find GC transactions used for THIS booking (redemptions are negative amounts)
                gc_transactions = locked_booking.gift_card_transactions.filter(
                    transaction_type='redemption'
                ).select_related('gift_card')

                for txn in gc_transactions:
                    if remaining_refund_needed <= 0:
                        break
                    
                    # The amount used is negative, so flip it
                    amount_used_from_card = abs(txn.amount)
                    
                    # Calculate how much to restore to this card
                    # We can't refund more to the card than was taken from it
                    refund_to_card = min(amount_used_from_card, remaining_refund_needed)
                    
                    if refund_to_card > 0:
                        gift_card = txn.gift_card
                        # Restore Balance
                        gift_card.current_balance += refund_to_card
                        gift_card.save()
                        
                        # Create Refund Transaction Log
                        from quickstart.models import GiftCardTransaction # Delayed import
                        GiftCardTransaction.objects.create(
                            gift_card=gift_card,
                            booking=locked_booking,
                            amount=refund_to_card,
                            balance_after=gift_card.current_balance,
                            transaction_type='refund'
                        )
                        
                        remaining_refund_needed -= refund_to_card
                        logger.info(f"Refunded ${refund_to_card} to Gift Card {gift_card.code}")

                # 3. Handle Stripe Refunds (If money is still owed)
                if remaining_refund_needed > 0:
                    payment = locked_booking.payments.filter(
                        status__in=["succeeded", "partially_refunded"]
                    ).first()

                    if payment and payment.available_refund_amount > 0:
                        # Cap refund at what is available in Stripe
                        refund_to_stripe = min(remaining_refund_needed, payment.available_refund_amount)
                        
                        if refund_to_stripe >= Decimal("0.50"):
                            stripe.Refund.create(
                                payment_intent=payment.stripe_payment_intent_id,
                                amount=int(refund_to_stripe * 100),
                                reason="requested_by_customer",
                            )
                            
                            payment.refunded_amount += refund_to_stripe
                            if payment.available_refund_amount <= Decimal("0.00"):
                                payment.status = "refunded"
                            else:
                                payment.status = "partially_refunded"
                            payment.save()
                            
                            remaining_refund_needed -= refund_to_stripe
                            logger.info(f"Refunded ${refund_to_stripe} to Stripe PI {payment.stripe_payment_intent_id}")
                        else:
                            logger.info(f"Remaining Stripe refund ${refund_to_stripe} too small to process. Forfeited.")
                    else:
                        logger.warning(f"Booking {booking.id}: Need to refund ${remaining_refund_needed} but no Stripe funds available.")

                # 4. Finalize Booking Status
                if remaining_refund_needed > 0 and remaining_refund_needed < Decimal("0.50"):
                     # We processed everything possible, small dust remaining is ignored
                     locked_booking.payment_status = "refunded"
                elif remaining_refund_needed > 0:
                     # We couldn't refund everything (e.g., Stripe limit reached logic error)
                     locked_booking.payment_status = "refund_failed"
                     logger.error(f"Booking {booking.id}: Could not fully refund. Short by ${remaining_refund_needed}")
                else:
                     locked_booking.payment_status = "refunded"

                locked_booking.save(update_fields=["payment_status"])
                successful_refunds += 1

                # Alert and notify guest when refund failed
                if locked_booking.payment_status == "refund_failed":
                    _on_cancellation_refund_failed(
                        locked_booking,
                        f"Could not fully refund (short by ${remaining_refund_needed}). Manual review required.",
                    )

        except Exception as e:
            logger.error(f"✗ ERROR refunding Booking {booking.id}: {e}", exc_info=True)
            failed_refunds += 1
            _on_cancellation_refund_failed(booking, str(e))

    return f"Refunds processed. Success: {successful_refunds}, Failed: {failed_refunds}"