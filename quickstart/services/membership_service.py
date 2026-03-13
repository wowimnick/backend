"""
Customer membership subscription service: Stripe Product/Price for MembershipProduct,
create customer subscription, credit ledger helpers.
"""
import logging
from datetime import datetime
from decimal import Decimal

import stripe
from django.conf import settings
from django.utils import timezone

from django.db.models import Sum

from quickstart.models import (
    Contact,
    CustomerMembership,
    MembershipCreditLedger,
    MembershipProduct,
)

logger = logging.getLogger(__name__)


def create_stripe_price(product):
    """
    Create or ensure Stripe Product + Price for this MembershipProduct.
    Idempotent: if product.stripe_price_id is set, returns it without creating again.
    Returns the Stripe Price id (product.stripe_price_id after save).
    """
    if product.stripe_price_id:
        try:
            stripe.Price.retrieve(product.stripe_price_id)
            return product.stripe_price_id
        except stripe.StripeError:
            pass

    stripe.api_key = settings.STRIPE_SECRET_KEY
    interval = product.billing_interval if product.billing_interval in ("month", "year") else "month"
    # Stripe unit_amount is in smallest currency unit (cents for CAD/USD)
    unit_amount = int(product.price * 100)

    # Create Stripe Product for this membership (one product per MembershipProduct)
    stripe_product = stripe.Product.create(
        name=product.name,
        description=(product.description or "")[:500] or None,
        metadata={"membership_product_id": str(product.id), "business_id": str(product.business_id)},
    )

    # Create recurring Price
    stripe_price = stripe.Price.create(
        product=stripe_product.id,
        unit_amount=unit_amount,
        currency=(product.currency or "cad").lower(),
        recurring={"interval": interval},
        metadata={"membership_product_id": str(product.id)},
    )

    product.stripe_price_id = stripe_price.id
    product.save(update_fields=["stripe_price_id"])
    return product.stripe_price_id


def create_customer_membership_subscription(
    business, product, email, first_name, last_name, payment_method_id=None
):
    """
    Create Stripe Customer + Subscription for a customer membership.
    Creates or gets Contact, creates CustomerMembership with status=incomplete,
    creates Stripe Subscription in default_incomplete mode.
    Returns (customer_membership, client_secret). client_secret may be None if invoice already paid.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY
    price_id = product.stripe_price_id
    if not price_id:
        price_id = create_stripe_price(product)

    # Get or create Contact for this business + email
    contact, _ = Contact.objects.get_or_create(
        business=business,
        email__iexact=email,
        defaults={
            "first_name": first_name or "",
            "last_name": last_name or "",
            "email": email,
            "source": "widget_booking",
        },
    )
    if contact.first_name != (first_name or "") or contact.last_name != (last_name or ""):
        contact.first_name = first_name or contact.first_name
        contact.last_name = last_name or contact.last_name
        contact.save(update_fields=["first_name", "last_name"])

    # Create or get Stripe Customer for this contact (one per contact per platform)
    stripe_customer_id = None
    if not stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=email,
            name=f"{first_name or ''} {last_name or ''}".strip() or email,
            metadata={
                "business_id": str(business.businessId),
                "membership_product_id": str(product.id),
                "contact_id": str(contact.id),
            },
        )
        stripe_customer_id = stripe_customer.id

    create_params = {
        "customer": stripe_customer_id,
        "items": [{"price": price_id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["latest_invoice.payment_intent", "latest_invoice.payments"],
        "metadata": {
            "business_id": str(business.businessId),
            "membership_product_id": str(product.id),
            "contact_id": str(contact.id),
        },
    }
    if payment_method_id:
        create_params["default_payment_method"] = payment_method_id

    stripe_sub = stripe.Subscription.create(**create_params)

    # Persist CustomerMembership so webhook can update it
    period_start = None
    period_end = None
    if getattr(stripe_sub, "current_period_start", None):
        period_start = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_start)
        )
    if getattr(stripe_sub, "current_period_end", None):
        period_end = timezone.make_aware(
            datetime.utcfromtimestamp(stripe_sub.current_period_end)
        )

    customer_membership = CustomerMembership.objects.create(
        product=product,
        contact=contact,
        user=None,
        stripe_subscription_id=stripe_sub.id,
        stripe_customer_id=stripe_customer_id,
        status="incomplete",
        current_period_start=period_start,
        current_period_end=period_end,
        source="widget",
    )

    # Extract client_secret from latest_invoice's payment_intent
    client_secret = None
    latest_invoice = getattr(stripe_sub, "latest_invoice", None) or (
        stripe_sub.get("latest_invoice") if isinstance(stripe_sub, dict) else None
    )
    if latest_invoice and not isinstance(latest_invoice, str):
        pi = getattr(latest_invoice, "payment_intent", None) or (
            latest_invoice.get("payment_intent") if isinstance(latest_invoice, dict) else None
        )
        if pi and not isinstance(pi, str):
            client_secret = getattr(pi, "client_secret", None) or (
                pi.get("client_secret") if isinstance(pi, dict) else None
            )
        if not client_secret and getattr(latest_invoice, "payment_intent", None):
            try:
                pi_id = (
                    latest_invoice.payment_intent
                    if isinstance(latest_invoice.payment_intent, str)
                    else getattr(latest_invoice.payment_intent, "id", None)
                )
                if pi_id:
                    pi_obj = stripe.PaymentIntent.retrieve(pi_id)
                    client_secret = getattr(pi_obj, "client_secret", None)
            except stripe.StripeError:
                pass

    return customer_membership, client_secret


def get_credits_remaining(membership, period_start=None):
    """
    Return number of credits remaining for this membership in the given period.
    period_start should be a date; if None, use membership.current_period_start date.
    Only meaningful when product.access_type == 'credits'.
    """
    if membership.product.access_type != "credits" or not membership.product.credit_allowance:
        return None
    period = period_start
    if period is None and membership.current_period_start:
        period = membership.current_period_start.date()
    if period is None:
        return membership.product.credit_allowance
    used = (
        MembershipCreditLedger.objects.filter(
            membership=membership,
            period_start=period,
            action="consumed",
        ).aggregate(total=Sum("credits_used"))["total"]
        or 0
    )
    allowance = membership.product.credit_allowance or 0
    return max(0, allowance - used)


def consume_credit(membership, booking, period_start=None):
    """
    Consume one credit (or more) for this membership and link to the booking.
    Raises ValueError if no credits remaining.
    """
    remaining = get_credits_remaining(membership, period_start)
    if remaining is None:
        return
    if remaining <= 0:
        raise ValueError("No credits remaining for this membership period")
    period = period_start
    if period is None and membership.current_period_start:
        period = membership.current_period_start.date()
    if period is None:
        raise ValueError("Membership has no period start; cannot consume credit")
    MembershipCreditLedger.objects.create(
        membership=membership,
        booking=booking,
        period_start=period,
        credits_used=1,
        action="consumed",
    )


def reset_period_credits(membership, period_start):
    """
    Record a ledger entry for period reset (e.g. on invoice.paid for new period).
    Does not change allowance; just logs the reset action. Optional.
    period_start: date or datetime for the new period start.
    """
    if hasattr(period_start, "date"):
        period_start = period_start.date()
    MembershipCreditLedger.objects.create(
        membership=membership,
        booking=None,
        period_start=period_start,
        credits_used=0,
        action="reset",
    )
