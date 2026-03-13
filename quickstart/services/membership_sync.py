"""
Sync customer membership subscription from Stripe (invoice.paid webhook).
Updates CustomerMembership, records MembershipPayment with platform fee, resets credits.
"""
import logging
from datetime import datetime
from decimal import Decimal

import pytz
import stripe
from django.conf import settings
from django.utils import timezone

from quickstart.models import CustomerMembership, MembershipPayment
from quickstart.services.membership_service import reset_period_credits
from quickstart.views.widget.widget_views import (
    WIDGET_PLAN_FEE_PERCENT,
    _get_widget_plan_fee_percentage,
)

logger = logging.getLogger(__name__)


def _subscription_to_period(sub, key):
    """Extract current_period_start or current_period_end from Stripe subscription."""
    raw = sub.get(key) if isinstance(sub, dict) else getattr(sub, key, None)
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return timezone.make_aware(datetime.utcfromtimestamp(raw))
    return raw


def sync_customer_membership_from_stripe(
    stripe_subscription_id, subscription_obj=None, invoice_obj=None
):
    """
    Sync a customer membership from Stripe (invoice.paid).
    - Updates CustomerMembership status and period.
    - If invoice_obj is provided: creates MembershipPayment with platform fee, resets credits for new period.
    Returns (CustomerMembership or None, error_message or None).
    """
    if not stripe_subscription_id:
        return None, "Missing stripe_subscription_id"
    try:
        if subscription_obj is None:
            subscription_obj = stripe.Subscription.retrieve(
                stripe_subscription_id,
                expand=["items.data.price"],
            )
    except stripe.StripeError as e:
        logger.warning(
            "membership_sync: Stripe retrieve failed sub_id=%s err=%s",
            stripe_subscription_id,
            e,
        )
        return None, str(e)

    meta = (
        subscription_obj.get("metadata")
        if isinstance(subscription_obj, dict)
        else getattr(subscription_obj, "metadata", None)
    ) or {}
    if not meta.get("membership_product_id"):
        return None, "Not a customer membership subscription"

    try:
        membership = CustomerMembership.objects.get(
            stripe_subscription_id=stripe_subscription_id
        )
    except CustomerMembership.DoesNotExist:
        logger.warning(
            "membership_sync: CustomerMembership not found for sub_id=%s",
            stripe_subscription_id,
        )
        return None, "CustomerMembership not found"

    business = membership.product.business
    status_str = (
        subscription_obj.get("status")
        if isinstance(subscription_obj, dict)
        else getattr(subscription_obj, "status", None)
    )
    status_str = (status_str or "active").strip().lower()
    current_period_start = _subscription_to_period(
        subscription_obj, "current_period_start"
    )
    current_period_end = _subscription_to_period(
        subscription_obj, "current_period_end"
    )
    cancel_at_period_end = bool(
        subscription_obj.get("cancel_at_period_end")
        or getattr(subscription_obj, "cancel_at_period_end", False)
    )
    customer_id = (
        subscription_obj.get("customer")
        or getattr(subscription_obj, "customer", None)
    )
    stripe_customer_id = str(customer_id) if customer_id else None

    membership.status = status_str
    membership.current_period_start = current_period_start
    membership.current_period_end = current_period_end
    membership.cancel_at_period_end = cancel_at_period_end
    if stripe_customer_id:
        membership.stripe_customer_id = stripe_customer_id
    membership.save()

    if invoice_obj is not None:
        # Record payment and platform fee
        amount_paid = (
            getattr(invoice_obj, "amount_paid", None)
            or invoice_obj.get("amount_paid")
            or 0
        )
        amount_decimal = Decimal(amount_paid) / 100
        fee_pct = _get_widget_plan_fee_percentage(business)
        platform_fee = (amount_decimal * (fee_pct / Decimal("100"))).quantize(
            Decimal("0.01")
        )
        net_payout = amount_decimal - platform_fee
        if net_payout < 0:
            net_payout = Decimal("0.00")
        invoice_id = (
            getattr(invoice_obj, "id", None) or invoice_obj.get("id") or ""
        )
        if invoice_id and not MembershipPayment.objects.filter(
            stripe_invoice_id=invoice_id
        ).exists():
            MembershipPayment.objects.create(
                membership=membership,
                stripe_invoice_id=invoice_id,
                stripe_payment_intent_id=(
                    getattr(invoice_obj, "payment_intent", None)
                    or invoice_obj.get("payment_intent")
                ),
                amount=amount_decimal,
                platform_fee_amount=platform_fee,
                net_payout_amount=net_payout,
                status="paid",
                period_start=current_period_start,
                period_end=current_period_end,
            )
        # Reset credits for the new period (for credit-based products)
        if (
            membership.product.access_type == "credits"
            and current_period_start
        ):
            try:
                reset_period_credits(
                    membership, current_period_start.date()
                )
            except Exception as e:
                logger.warning(
                    "membership_sync: reset_period_credits failed: %s", e
                )

    return membership, None
