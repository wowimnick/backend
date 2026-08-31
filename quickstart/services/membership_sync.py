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
from quickstart.utils.commission import get_plan_fee_percentage

logger = logging.getLogger(__name__)


def _stripe_dict_get(obj, key, default=None):
    """Read key from dict or Stripe StripeObject (StripeObject has no .get)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj[key]
    except (KeyError, TypeError, AttributeError):
        pass
    return getattr(obj, key, default)


def _subscription_to_period(sub, key):
    """Extract current_period_start or current_period_end from Stripe subscription or item."""
    raw = _stripe_dict_get(sub, key)
    if raw is None:
        return None
    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, (int, float)):
        return timezone.make_aware(datetime.utcfromtimestamp(int(raw)))
    return raw


def _subscription_current_period_bounds(subscription_obj):
    """
    Stripe returns current_period_start / current_period_end as Unix timestamps (seconds) on
    the Subscription object (see Stripe API: Subscription object). If either is missing on
    the parent, fall back to the first subscription item (items.data[0]), which also exposes
    current_period_start / current_period_end per item.
    """
    start = _subscription_to_period(subscription_obj, "current_period_start")
    end = _subscription_to_period(subscription_obj, "current_period_end")
    if start is not None and end is not None:
        return start, end
    items = _stripe_dict_get(subscription_obj, "items")
    inner = _stripe_dict_get(items, "data") if items is not None else None
    if not inner:
        return start, end
    first = inner[0] if isinstance(inner, (list, tuple)) else None
    if first is None:
        return start, end
    if start is None:
        start = _subscription_to_period(first, "current_period_start")
    if end is None:
        end = _subscription_to_period(first, "current_period_end")
    return start, end


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

    meta = _stripe_dict_get(subscription_obj, "metadata")
    if not _stripe_dict_get(meta, "membership_product_id"):
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
    raw_status = _stripe_dict_get(subscription_obj, "status")
    status_str = (raw_status or "active").strip().lower()
    current_period_start, current_period_end = _subscription_current_period_bounds(
        subscription_obj
    )
    cancel_at_period_end = bool(
        _stripe_dict_get(subscription_obj, "cancel_at_period_end", False)
    )
    customer_id = _stripe_dict_get(subscription_obj, "customer")
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
        amount_paid = _stripe_dict_get(invoice_obj, "amount_paid") or 0
        amount_decimal = Decimal(amount_paid) / 100
        fee_pct = get_plan_fee_percentage(business)
        platform_fee = (amount_decimal * (fee_pct / Decimal("100"))).quantize(
            Decimal("0.01")
        )
        net_payout = amount_decimal - platform_fee
        if net_payout < 0:
            net_payout = Decimal("0.00")
        invoice_id = _stripe_dict_get(invoice_obj, "id") or ""
        if invoice_id and not MembershipPayment.objects.filter(
            stripe_invoice_id=invoice_id
        ).exists():
            MembershipPayment.objects.create(
                membership=membership,
                stripe_invoice_id=invoice_id,
                stripe_payment_intent_id=_stripe_dict_get(
                    invoice_obj, "payment_intent"
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
