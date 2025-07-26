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

from quickstart.models import Booking, BusinessInfo, Payout

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

PLATFORM_FEE_RATE = Decimal("0.20")


@shared_task(name="tasks.update_completed_booking_status")
def update_completed_booking_status():
    # ... (this function is correct, no changes needed)
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
    # ... (parts of this function are the same)
    payout_date_for_classes = timezone.now().date() - timedelta(days=1)
    logger.info(
        f"--- Starting daily payout process for classes completed on: {payout_date_for_classes} ---"
    )

    bookings_to_payout = Booking.objects.filter(
        schedule_instance__date=payout_date_for_classes,
        status="completed",
        payment_status="paid",
        payout_status="pending",
    ).select_related("schedule_instance__schedule__option__classId__businessId")

    if not bookings_to_payout.exists():
        logger.info(
            "No bookings found requiring payout for the target date. Process finished."
        )
        return "No bookings to pay out."

    payouts_by_business = {}
    for booking in bookings_to_payout:
        try:
            business_account_id = (
                booking.schedule_instance.schedule.option.classId.businessId.stripe_account_id
            )
            if business_account_id:
                if business_account_id not in payouts_by_business:
                    payouts_by_business[business_account_id] = {
                        "business_instance": booking.schedule_instance.schedule.option.classId.businessId,
                        "total_gross_revenue": Decimal("0.0"),
                        "booking_ids": [],
                    }
                payouts_by_business[business_account_id][
                    "total_gross_revenue"
                ] += booking.amount_paid
                payouts_by_business[business_account_id]["booking_ids"].append(
                    booking.id
                )
        except Exception as e:
            logger.error(
                f"Error processing booking {booking.id} when grouping for payout: {e}",
                exc_info=True,
            )
            continue

    logger.info(f"Found {len(payouts_by_business)} businesses to process payouts for.")

    successful_payouts = 0
    failed_payouts = 0

    for stripe_id, data in payouts_by_business.items():
        business = data["business_instance"]
        total_gross = data["total_gross_revenue"]
        booking_ids_for_payout = data["booking_ids"]

        try:
            with transaction.atomic():
                platform_fee = (total_gross * PLATFORM_FEE_RATE).quantize(
                    Decimal("0.01")
                )
                net_payout_amount = total_gross - platform_fee

                if net_payout_amount <= Decimal("0.50"):
                    logger.warning(
                        f"Skipping payout for Business {business.businessId} as net amount ${net_payout_amount} is too low."
                    )
                    Booking.objects.filter(id__in=booking_ids_for_payout).update(
                        payout_status="processed"
                    )
                    continue

                payout_amount_cents = int(net_payout_amount * 100)
                logger.info(
                    f"Attempting to transfer ${net_payout_amount} to Business {business.businessId} (Stripe ID: {stripe_id})"
                )

                transfer = stripe.Transfer.create(
                    amount=payout_amount_cents,
                    currency=getattr(settings, "STRIPE_CURRENCY", "usd").lower(),
                    destination=stripe_id,
                    description=f"ClassEasily Payout for classes on {payout_date_for_classes}",
                    metadata={
                        "classeasily_business_id": business.businessId,
                        "payout_date_for_classes": str(payout_date_for_classes),
                        "included_booking_count": len(booking_ids_for_payout),
                        "gross_revenue": str(total_gross),
                        "platform_fee": str(platform_fee),
                    },
                )

                # --- DEBUGGING STEP: Print the entire transfer object ---
                logger.info("--- STRIPE TRANSFER OBJECT RESPONSE ---")
                logger.info(transfer)
                logger.info("------------------------------------")
                # --- END DEBUGGING STEP ---

                # --- FINAL FIX: Use getattr for ALL attributes for robustness ---
                transfer_id = getattr(transfer, "id", None)
                transfer_currency = getattr(transfer, "currency", "usd")
                transfer_status = getattr(
                    transfer, "status", "unknown"
                )  # This is the key fix
                arrival_date_timestamp = getattr(transfer, "arrival_date", None)

                if not transfer_id:
                    # If we don't even have an ID, something is very wrong.
                    raise ValueError("Stripe API did not return a transfer ID.")

                if arrival_date_timestamp:
                    payout_arrival_date = datetime.fromtimestamp(
                        arrival_date_timestamp, tz=pytz.utc
                    ).date()
                else:
                    logger.warning(
                        f"Stripe Transfer {transfer_id} did not include an arrival_date. Estimating a fallback date."
                    )
                    payout_arrival_date = timezone.now().date() + timedelta(days=3)

                payout_record = Payout.objects.create(
                    business=business,
                    stripe_transfer_id=transfer_id,
                    amount=net_payout_amount,
                    currency=transfer_currency.upper(),
                    arrival_date=payout_arrival_date,
                    status=transfer_status,  # Use the safely-retrieved status
                )

                bookings_in_payout = Booking.objects.filter(
                    id__in=booking_ids_for_payout
                )
                payout_record.bookings.set(bookings_in_payout)
                bookings_in_payout.update(payout_status="processed")

                logger.info(
                    f"SUCCESS: Created Stripe Transfer {transfer_id} and Payout Record {payout_record.id} for Business {business.businessId}."
                )
                successful_payouts += 1

        except stripe.StripeError as e:
            logger.error(
                f"STRIPE ERROR processing payout for Business {business.businessId}: {e}",
                exc_info=True,
            )
            Booking.objects.filter(id__in=booking_ids_for_payout).update(
                payout_status="failed"
            )
            failed_payouts += 1
        except Exception as e:
            logger.error(
                f"UNEXPECTED ERROR processing payout for Business {business.businessId}: {e}",
                exc_info=True,
            )
            Booking.objects.filter(id__in=booking_ids_for_payout).update(
                payout_status="failed"
            )
            failed_payouts += 1

    summary = f"Payout process finished. Successful transfers: {successful_payouts}. Failed transfers: {failed_payouts}."
    logger.info(summary)
    return summary
