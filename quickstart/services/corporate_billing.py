"""Stripe Customer, deposit PaymentIntent, balance Invoice for corporate bookings."""

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, Optional

import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from quickstart.models import CorporateBooking

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


def _customer_search_by_email(email: str) -> Optional[str]:
    if not email:
        return None
    try:
        res = stripe.Customer.search(
            query=f"email:'{email.replace(chr(39), chr(92)+chr(39))}'",
            limit=1,
        )
        data = getattr(res, "data", None) or []
        if data:
            return data[0].id
    except Exception as e:
        logger.warning("Stripe customer search failed: %s", e)
    return None


@transaction.atomic
def ensure_stripe_customer(booking: CorporateBooking) -> str:
    """
    Reuse stored id, or find/create by billing email, save on booking.
    """
    if booking.stripe_customer_id:
        return booking.stripe_customer_id
    email = (booking.billing_email or "").strip().lower()
    cid = _customer_search_by_email(email)
    if not cid:
        cust = stripe.Customer.create(
            email=email,
            name=booking.billing_contact_name or None,
            metadata={
                "type": "corporate_booking",
                "corporate_booking_id": str(booking.id),
            },
        )
        cid = cust.id
    booking.stripe_customer_id = cid
    booking.save(update_fields=["stripe_customer_id", "updated_at"])
    return cid


def create_or_reuse_deposit_payment_intent(booking: CorporateBooking) -> Dict[str, Any]:
    """
    Create Stripe PaymentIntent for deposit. Reuses in-flight PI if still usable.
    """
    if booking.status != CorporateBooking.ST_PENDING:
        raise ValueError("Booking is not awaiting deposit.")
    if booking.deposit_cents <= 0:
        raise ValueError("Invalid deposit amount.")

    customer_id = ensure_stripe_customer(booking)

    if booking.deposit_payment_intent_id:
        try:
            existing = stripe.PaymentIntent.retrieve(booking.deposit_payment_intent_id)
            if existing.status in ("requires_payment_method", "requires_confirmation", "requires_action", "processing"):
                return {
                    "client_secret": existing.client_secret,
                    "payment_intent_id": existing.id,
                    "publishable_key": settings.STRIPE_PUBLIC_KEY,
                }
        except stripe.error.StripeError:
            pass

    intent = stripe.PaymentIntent.create(
        amount=int(booking.deposit_cents),
        currency=booking.currency,
        customer=customer_id,
        metadata={
            "type": "corporate_deposit",
            "corporate_booking_id": str(booking.id),
        },
        automatic_payment_methods={"enabled": True},
    )
    booking.deposit_payment_intent_id = intent.id
    booking.save(update_fields=["deposit_payment_intent_id", "updated_at"])
    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "publishable_key": settings.STRIPE_PUBLIC_KEY,
    }


def create_balance_invoice(booking: CorporateBooking, due_in_days: int = 15) -> Dict[str, Any]:
    """
    Create Stripe Invoice for remaining balance, finalize, and email the customer.
    """
    if booking.status not in (
        CorporateBooking.ST_DEPOSIT_PAID,
    ):
        raise ValueError("Deposit must be paid before issuing balance invoice.")
    if booking.balance_cents <= 0:
        raise ValueError("No balance due.")
    if booking.stripe_invoice_id:
        raise ValueError("Invoice already issued for this booking.")

    customer_id = ensure_stripe_customer(booking)
    booking = CorporateBooking.objects.select_related("shortlist__inquiry").get(pk=booking.pk)
    meta = {
        "type": "corporate_balance",
        "corporate_booking_id": str(booking.id),
    }
    company = booking.shortlist.inquiry.company_name if booking.shortlist else ""

    inv = stripe.Invoice.create(
        customer=customer_id,
        collection_method="send_invoice",
        days_until_due=due_in_days,
        metadata=meta,
        auto_advance=False,
    )
    stripe.InvoiceItem.create(
        customer=customer_id,
        invoice=inv.id,
        amount=int(booking.balance_cents),
        currency=booking.currency,
        description=f"Event balance — {booking.reference} ({company})",
        metadata=meta,
    )
    inv = stripe.Invoice.finalize_invoice(inv.id)
    # Email hosted invoice to customer
    try:
        inv = stripe.Invoice.send_invoice(inv.id)
    except stripe.error.StripeError as e:
        logger.warning("send_invoice failed (invoice still created): %s", e)

    hosted = getattr(inv, "hosted_invoice_url", None) or inv.get("hosted_invoice_url", "")
    booking.stripe_invoice_id = inv.id
    booking.invoice_url = hosted or ""
    booking.invoice_status = getattr(inv, "status", None) or "open"
    due_ts = getattr(inv, "due_date", None)
    if due_ts:
        booking.invoice_due_at = datetime.fromtimestamp(
            int(due_ts), tz=dt_timezone.utc
        )
    booking.status = CorporateBooking.ST_INVOICED
    booking.save(
        update_fields=[
            "stripe_invoice_id",
            "invoice_url",
            "invoice_status",
            "invoice_due_at",
            "status",
            "updated_at",
        ]
    )
    return {
        "invoice_id": inv.id,
        "invoice_url": booking.invoice_url,
        "status": booking.invoice_status,
    }


def refund_deposit(booking: CorporateBooking, reason: str = "requested_by_admin") -> Optional[str]:
    if not booking.deposit_payment_intent_id:
        return None
    try:
        intent = stripe.PaymentIntent.retrieve(booking.deposit_payment_intent_id)
        ch_id = intent.charges.data[0].id if intent.charges and intent.charges.data else None
        if ch_id:
            re = stripe.Refund.create(charge=ch_id, metadata={"reason": reason})
            return re.id
    except stripe.error.StripeError as e:
        logger.exception("Refund failed: %s", e)
        raise
    return None
