"""Stripe webhook handlers for corporate deposit PI and balance Invoice."""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from quickstart.models import CorporateBooking, ProcessedStripeEvent
from quickstart.utils.corporate_events import log_corporate_booking_event
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict

logger = logging.getLogger(__name__)


def _meta(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return stripe_metadata_to_dict(obj.get("metadata"))
    return stripe_metadata_to_dict(getattr(obj, "metadata", None))


@transaction.atomic
def handle_corporate_deposit_succeeded(payment_intent, stripe_event_id, webhook_id):
    """Idempotent: mark corporate booking deposit paid, notify. Caller must verify type."""
    if stripe_event_id and ProcessedStripeEvent.objects.filter(
        event_id=stripe_event_id
    ).exists():
        return Response(status=status.HTTP_200_OK)

    meta = _meta(payment_intent)
    bid = meta.get("corporate_booking_id")
    if not bid:
        logger.error("[%s] corporate deposit: missing corporate_booking_id", webhook_id)
        return Response(
            {"error": "Missing corporate_booking_id"}, status=status.HTTP_400_BAD_REQUEST
        )
    try:
        booking = CorporateBooking.objects.select_for_update().get(pk=bid)
    except CorporateBooking.DoesNotExist:
        logger.error("[%s] corporate deposit: booking %s not found", webhook_id, bid)
        return Response(status=status.HTTP_200_OK)

    if booking.status == CorporateBooking.ST_DEPOSIT_PAID:
        if stripe_event_id:
            ProcessedStripeEvent.objects.get_or_create(
                event_id=stripe_event_id,
                defaults={"event_type": "payment_intent.succeeded"},
            )
        return Response(status=status.HTTP_200_OK)

    if booking.status != CorporateBooking.ST_PENDING:
        logger.warning(
            "[%s] corporate deposit: booking %s status %s",
            webhook_id,
            bid,
            booking.status,
        )

    now = timezone.now()
    booking.status = CorporateBooking.ST_DEPOSIT_PAID
    booking.deposit_paid_at = now
    pi_id = getattr(payment_intent, "id", None) or (
        payment_intent.get("id") if isinstance(payment_intent, dict) else None
    )
    if pi_id:
        booking.deposit_payment_intent_id = str(pi_id)
    booking.save(
        update_fields=["status", "deposit_paid_at", "deposit_payment_intent_id", "updated_at"]
    )
    log_corporate_booking_event(
        booking,
        "deposit_paid",
        "Deposit confirmed via Stripe.",
    )
    if stripe_event_id:
        ProcessedStripeEvent.objects.get_or_create(
            event_id=stripe_event_id,
            defaults={"event_type": "payment_intent.succeeded"},
        )
    try:
        from quickstart.tasks.corporate_booking_tasks import send_deposit_paid
        send_deposit_paid.delay(str(booking.id))
    except Exception as e:
        logger.exception("[%s] queue deposit email: %s", webhook_id, e)
    return Response(status=status.HTTP_200_OK)


@transaction.atomic
def handle_corporate_balance_invoice_paid(invoice_obj, stripe_event_id, webhook_id):
    """Return Response if handled, None if not a corporate balance invoice."""
    meta = _meta(invoice_obj)
    if meta.get("type") != "corporate_balance":
        return None
    bid = meta.get("corporate_booking_id")
    if not bid:
        return None

    if stripe_event_id and ProcessedStripeEvent.objects.filter(
        event_id=stripe_event_id
    ).exists():
        return Response(status=status.HTTP_200_OK)

    try:
        booking = CorporateBooking.objects.select_for_update().get(pk=bid)
    except CorporateBooking.DoesNotExist:
        logger.error(
            "[%s] corporate balance paid: booking %s not found", webhook_id, bid
        )
        return Response(status=status.HTTP_200_OK)

    if booking.status == CorporateBooking.ST_FULLY_PAID:
        if stripe_event_id:
            ProcessedStripeEvent.objects.get_or_create(
                event_id=stripe_event_id,
                defaults={"event_type": "invoice.paid"},
            )
        return Response(status=status.HTTP_200_OK)

    now = timezone.now()
    booking.status = CorporateBooking.ST_FULLY_PAID
    booking.balance_paid_at = now
    booking.invoice_status = "paid"
    inv_id = getattr(invoice_obj, "id", None) if not isinstance(
        invoice_obj, dict
    ) else invoice_obj.get("id")
    if inv_id and not booking.stripe_invoice_id:
        booking.stripe_invoice_id = str(inv_id)
    hi = (
        getattr(invoice_obj, "hosted_invoice_url", None)
        if not isinstance(invoice_obj, dict)
        else invoice_obj.get("hosted_invoice_url")
    )
    if hi:
        booking.invoice_url = hi or booking.invoice_url
    booking.save(
        update_fields=[
            "status",
            "balance_paid_at",
            "invoice_status",
            "stripe_invoice_id",
            "invoice_url",
            "updated_at",
        ]
    )
    log_corporate_booking_event(
        booking,
        "balance_paid",
        "Balance invoice paid.",
    )
    if stripe_event_id:
        ProcessedStripeEvent.objects.get_or_create(
            event_id=stripe_event_id,
            defaults={"event_type": "invoice.paid"},
        )
    try:
        from quickstart.tasks.corporate_booking_tasks import send_balance_paid
        send_balance_paid.delay(str(booking.id))
    except Exception as e:
        logger.exception("[%s] queue balance paid email: %s", webhook_id, e)
    return Response(status=status.HTTP_200_OK)


@transaction.atomic
def handle_corporate_balance_pi_succeeded(payment_intent, stripe_event_id, webhook_id):
    """Idempotent: mark corporate booking fully paid via on-site balance PI."""
    if stripe_event_id and ProcessedStripeEvent.objects.filter(
        event_id=stripe_event_id
    ).exists():
        return Response(status=status.HTTP_200_OK)

    meta = _meta(payment_intent)
    bid = meta.get("corporate_booking_id")
    if not bid:
        logger.error(
            "[%s] corporate balance pi: missing corporate_booking_id", webhook_id
        )
        return Response(
            {"error": "Missing corporate_booking_id"}, status=status.HTTP_400_BAD_REQUEST
        )
    try:
        booking = CorporateBooking.objects.select_for_update().get(pk=bid)
    except CorporateBooking.DoesNotExist:
        logger.error(
            "[%s] corporate balance pi: booking %s not found", webhook_id, bid
        )
        return Response(status=status.HTTP_200_OK)

    if booking.status == CorporateBooking.ST_FULLY_PAID:
        if stripe_event_id:
            ProcessedStripeEvent.objects.get_or_create(
                event_id=stripe_event_id,
                defaults={"event_type": "payment_intent.succeeded"},
            )
        return Response(status=status.HTTP_200_OK)

    if booking.status not in (
        CorporateBooking.ST_DEPOSIT_PAID,
        CorporateBooking.ST_INVOICED,
    ):
        logger.warning(
            "[%s] corporate balance pi: booking %s status %s",
            webhook_id,
            bid,
            booking.status,
        )

    if booking.stripe_invoice_id and (booking.invoice_status or "") not in (
        "paid",
        "void",
    ):
        try:
            import stripe
            from django.conf import settings

            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.Invoice.void_invoice(booking.stripe_invoice_id)
            booking.invoice_status = "void"
        except Exception as e:
            logger.warning(
                "[%s] void invoice %s failed: %s",
                webhook_id,
                booking.stripe_invoice_id,
                e,
            )

    now = timezone.now()
    booking.status = CorporateBooking.ST_FULLY_PAID
    booking.balance_paid_at = now
    booking.invoice_status = booking.invoice_status or "paid"
    if booking.invoice_status == "void":
        booking.invoice_status = "paid"
    booking.save(
        update_fields=[
            "status",
            "balance_paid_at",
            "invoice_status",
            "updated_at",
        ]
    )
    log_corporate_booking_event(
        booking,
        "balance_paid",
        "Balance paid via on-site checkout.",
    )
    if stripe_event_id:
        ProcessedStripeEvent.objects.get_or_create(
            event_id=stripe_event_id,
            defaults={"event_type": "payment_intent.succeeded"},
        )
    try:
        from quickstart.tasks.corporate_booking_tasks import send_balance_paid
        send_balance_paid.delay(str(booking.id))
    except Exception as e:
        logger.exception("[%s] queue balance paid email: %s", webhook_id, e)
    return Response(status=status.HTTP_200_OK)
