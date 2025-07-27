# quickstart/tasks/payout_tasks.py

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

from quickstart.models import Booking, BusinessInfo, Payout, Payment

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


@shared_task(name="tasks.update_completed_booking_status")
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


@shared_task(name="tasks.process_daily_payouts")
def process_daily_payouts():
    """
    Calculates net daily revenue for businesses and initiates Stripe transfers.
    """
    logger.info("--- Starting Daily Payout Processing Task ---")

    # Correctly fetches completed bookings ready for payout.
    bookings_to_payout = Booking.objects.filter(
        status="completed",
        payment_status="paid",
        payout_status="pending",
    ).select_related(
        "schedule_instance__schedule__option__classId__businessId", "payments"
    )

    if not bookings_to_payout.exists():
        logger.info("No bookings found requiring payout. Process finished.")
        return "No bookings to pay out."

    # Group bookings by business to create consolidated payouts
    payouts_by_business = {}
    for booking in bookings_to_payout:
        business = booking.schedule_instance.schedule.option.classId.businessId
        if business.stripe_account_id:
            payouts_by_business.setdefault(
                business.stripe_account_id,
                {
                    "business_instance": business,
                    "net_revenue": Decimal("0.0"),
                    "booking_ids": [],
                },
            )

            payment = booking.payments.first()
            if payment:
                service_fee = payment.service_fee_amount or Decimal("0.00")
                net_amount = booking.amount_paid - service_fee
                payouts_by_business[business.stripe_account_id][
                    "net_revenue"
                ] += net_amount
                payouts_by_business[business.stripe_account_id]["booking_ids"].append(
                    booking.id
                )
            else:
                logger.warning(
                    f"Booking {booking.id} skipped for payout: no associated payment record found."
                )

    logger.info(f"Found {len(payouts_by_business)} businesses to process payouts for.")

    successful_payouts = 0
    failed_payouts = 0

    for stripe_id, data in payouts_by_business.items():
        business = data["business_instance"]
        net_payout_amount = data["net_revenue"]
        booking_ids = data["booking_ids"]

        if net_payout_amount <= Decimal("0.50"):
            logger.warning(
                f"Skipping payout for Business {business.businessId} as net amount ${net_payout_amount} is too low."
            )
            continue

        payout_record = None
        temp_transfer_id = f"temp_task_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{business.businessId}_{random.randint(1000, 9999)}"

        try:
            with transaction.atomic():
                payout_amount_cents = int(net_payout_amount * 100)

                # Create the payout record FIRST with 'pending' status and temporary ID
                payout_record = Payout.objects.create(
                    business=business,
                    stripe_transfer_id=temp_transfer_id,  # Temporary ID, will be updated after successful Stripe call
                    amount=net_payout_amount,
                    currency=business.currency.upper(),
                    arrival_date=timezone.now().date()
                    + timedelta(days=3),  # Default arrival date
                    status="pending",  # Start as pending
                    metadata={
                        "source": "daily_payout_task",
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "temp_id": True,  # Flag to indicate this started with a temp ID
                    },
                )

                # Try to create the Stripe transfer
                transfer = stripe.Transfer.create(
                    amount=payout_amount_cents,
                    currency=business.currency.lower(),
                    destination=stripe_id,
                    description=f"ClassEasily Payout",
                    metadata={
                        "business_id": business.businessId,
                        "booking_count": len(booking_ids),
                        "payout_record_id": payout_record.id,  # Link back to our record
                    },
                )

                # SUCCESS: Update the record with Stripe details
                arrival_date = (
                    datetime.fromtimestamp(transfer.arrival_date, tz=pytz.utc).date()
                    if transfer.arrival_date
                    else timezone.now().date() + timedelta(days=3)
                )

                # Update metadata to remove temp flag and add real Stripe data
                updated_metadata = transfer.metadata.copy()
                updated_metadata.pop("temp_id", None)  # Remove temp flag

                payout_record.stripe_transfer_id = (
                    transfer.id
                )  # Replace temp ID with real Stripe ID
                payout_record.arrival_date = arrival_date
                payout_record.status = (
                    "paid"  # Hardcode to success since API call succeeded
                )
                payout_record.metadata = updated_metadata
                payout_record.save(
                    update_fields=[
                        "stripe_transfer_id",
                        "arrival_date",
                        "status",
                        "metadata",
                    ]
                )

                # Associate bookings and mark as processed
                bookings = Booking.objects.filter(id__in=booking_ids)
                payout_record.bookings.set(bookings)
                bookings.update(payout_status="processed")

                successful_payouts += 1
                logger.info(
                    f"SUCCESS: Created Stripe Transfer {transfer.id} for Business {business.businessId}. Payout record {payout_record.id} updated: temp ID '{temp_transfer_id}' → real ID '{transfer.id}', status → 'paid'."
                )

        except stripe.error.StripeError as stripe_error:
            # STRIPE API FAILURE: Mark as failed
            failed_payouts += 1
            if payout_record:
                payout_record.status = "failed"
                payout_record.save(update_fields=["status"])
                logger.error(
                    f"STRIPE ERROR for Business {business.businessId}: {stripe_error}. Payout record {payout_record.id} marked as 'failed'."
                )
            else:
                logger.error(
                    f"STRIPE ERROR for Business {business.businessId}: {stripe_error}. No payout record created."
                )

        except Exception as general_error:
            # GENERAL FAILURE: Mark as failed
            failed_payouts += 1
            if payout_record:
                payout_record.status = "failed"
                payout_record.save(update_fields=["status"])
                logger.error(
                    f"GENERAL ERROR for Business {business.businessId}: {general_error}. Payout record {payout_record.id} marked as 'failed'.",
                    exc_info=True,
                )
            else:
                logger.error(
                    f"GENERAL ERROR for Business {business.businessId}: {general_error}. No payout record created.",
                    exc_info=True,
                )

    return f"Payout process finished. Successful: {successful_payouts}. Failed: {failed_payouts}."


@shared_task(name="tasks.process_daily_refunds")
def process_daily_refunds():
    """
    A daily Celery task to find bookings pending a refund and process them via Stripe.
    """
    logger.info("--- Starting Daily Refund Processing Task ---")

    bookings_to_refund = Booking.objects.filter(payment_status="refund_pending")

    if not bookings_to_refund.exists():
        logger.info("No bookings pending refund today.")
        return "No refunds to process."

    logger.info(f"Found {bookings_to_refund.count()} bookings to process for refunds.")
    successful_refunds, failed_refunds = 0, 0

    for booking in bookings_to_refund:
        try:
            with transaction.atomic():
                locked_booking = Booking.objects.select_for_update().get(id=booking.id)
                if locked_booking.payment_status != "refund_pending":
                    continue

                payment = locked_booking.payments.filter(
                    status__in=["succeeded", "partially_refunded"]
                ).first()
                if not payment:
                    logger.error(
                        f"Cannot process refund for Booking {locked_booking.id}: No successful payment record found."
                    )
                    locked_booking.payment_status = "refund_failed"
                    locked_booking.save(update_fields=["payment_status"])
                    failed_refunds += 1
                    continue

                refund_percentage = (
                    Decimal(locked_booking.cancellation_refund_percentage) / 100
                )
                amount_to_refund = payment.available_refund_amount * refund_percentage

                if amount_to_refund < Decimal("0.50"):
                    logger.warning(
                        f"Refund for Booking {locked_booking.id} is too small (${amount_to_refund}). Marking as refunded without transaction."
                    )
                    locked_booking.payment_status = "refunded"
                    locked_booking.save(update_fields=["payment_status"])
                    successful_refunds += 1
                    continue

                stripe.Refund.create(
                    payment_intent=payment.stripe_payment_intent_id,
                    amount=int(amount_to_refund * 100),
                    reason="customer_request",
                )

                locked_booking.payment_status = "refunded"
                locked_booking.save(update_fields=["payment_status"])
                successful_refunds += 1
                logger.info(
                    f"Successfully processed refund for Booking {locked_booking.id}."
                )

        except stripe.error.StripeError as e:
            logger.error(
                f"Stripe Error processing refund for Booking {booking.id}: {e}"
            )
            booking.payment_status = "refund_failed"
            booking.save(update_fields=["payment_status"])
            failed_refunds += 1
        except Exception as e:
            logger.error(
                f"Unexpected error processing refund for Booking {booking.id}: {e}",
                exc_info=True,
            )
            failed_refunds += 1
            # Let the transaction rollback, so it will be retried tomorrow

    return f"Refund process finished. Successful: {successful_refunds}. Failed: {failed_refunds}."
