"""
Subscription sync: Stripe is the single source of truth.
All subscription state (widget + addons) is synced FROM Stripe TO DB here.
Webhooks call these functions; API write endpoints call Stripe then sync.
"""
import logging
from datetime import datetime

import pytz
import stripe
from django.conf import settings
from django.db import DatabaseError

from quickstart.models import (
    BusinessAddonSubscription,
    BusinessInfo,
    WidgetSubscription,
)
from quickstart.models import ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING

logger = logging.getLogger(__name__)

VALID_PLAN_IDS = {"basic", "growth", "advanced"}


def _obj_get(obj, key, default=None):
    """Read a field from dict-like or StripeObject without calling .get on StripeObject."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _widget_price_to_plan_id(stripe_price_id):
    """Map Stripe price ID to plan_id. Returns None if unknown."""
    if not stripe_price_id:
        return None
    price_map = {
        getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None): "basic",
        getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None): "growth",
        getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None): "advanced",
    }
    return price_map.get(stripe_price_id)


def _subscription_to_period_end(sub):
    """Extract current_period_end datetime from Stripe subscription object."""
    raw = _obj_get(sub, "current_period_end")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=pytz.UTC)
    return raw


def sync_widget_subscription_from_stripe(stripe_subscription_id, subscription_obj=None):
    """
    Sync a single Stripe subscription (widget plan) to WidgetSubscription.
    Stripe is source of truth. Creates or updates DB row.
    subscription_obj: optional pre-fetched Stripe subscription (dict or object).
    Returns (WidgetSubscription or None, error_message or None).
    """
    if not stripe_subscription_id:
        return None, "Missing stripe_subscription_id"
    try:
        if subscription_obj is None:
            subscription_obj = stripe.Subscription.retrieve(
                stripe_subscription_id,
                expand=["items.data.price", "schedule", "schedule.phases"],
            )
    except stripe.StripeError as e:
        logger.warning("subscription_sync: Stripe retrieve failed sub_id=%s err=%s", stripe_subscription_id, e)
        return None, str(e)

    meta = _obj_get(subscription_obj, "metadata", {}) or {}
    business_id_str = meta.get("business_id")
    if not business_id_str:
        logger.warning("subscription_sync: No business_id in metadata sub_id=%s", stripe_subscription_id)
        return None, "Missing business_id in subscription metadata"
    try:
        business = BusinessInfo.objects.get(businessId=int(business_id_str))
    except (BusinessInfo.DoesNotExist, ValueError):
        logger.warning("subscription_sync: Business not found business_id=%s", business_id_str)
        return None, "Business not found"

    status = (_obj_get(subscription_obj, "status", "") or "").strip().lower() or "incomplete"
    current_period_end = _subscription_to_period_end(subscription_obj)
    cancel_at_period_end = bool(_obj_get(subscription_obj, "cancel_at_period_end", False))
    # When cancellation is driven by a schedule (end_behavior=cancel), Stripe may not set cancel_at_period_end on the subscription; derive it from the schedule.
    schedule_ref = _obj_get(subscription_obj, "schedule")
    if schedule_ref is not None:
        end_behavior = None
        phases = []
        if isinstance(schedule_ref, str):
            try:
                schedule = stripe.SubscriptionSchedule.retrieve(schedule_ref, expand=["phases"])
                end_behavior = _obj_get(schedule, "end_behavior")
                phases = _obj_get(schedule, "phases", []) or []
            except stripe.StripeError:
                pass
        else:
            end_behavior = _obj_get(schedule_ref, "end_behavior")
            phases = _obj_get(schedule_ref, "phases", []) or []
        if end_behavior == "cancel" and phases and len(phases) == 1:
            cancel_at_period_end = True

    items = _obj_get(_obj_get(subscription_obj, "items", {}) or {}, "data", []) or []
    stripe_price_id = None
    if items and items[0]:
        price = items[0].get("price") if isinstance(items[0], dict) else getattr(items[0], "price", None)
        if price is not None:
            stripe_price_id = (
                price if isinstance(price, str) else
                (price.get("id") if isinstance(price, dict) else getattr(price, "id", None))
            )

    plan_id = _widget_price_to_plan_id(stripe_price_id)
    if not plan_id:
        plan_id = (meta.get("plan_id") or "growth").strip().lower()
    if plan_id not in VALID_PLAN_IDS:
        plan_id = "growth"

    customer_id = _obj_get(subscription_obj, "customer")
    stripe_customer_id = str(customer_id) if customer_id else ""

    defaults = {
        "business": business,
        "plan_id": plan_id,
        "stripe_customer_id": stripe_customer_id,
        "stripe_price_id": stripe_price_id,
        "status": status,
        "current_period_end": current_period_end,
        "cancel_at_period_end": cancel_at_period_end,
    }

    # Lookup: by stripe_subscription_id first; if not found, by business (webhook may arrive before API set it).
    existing = WidgetSubscription.objects.filter(stripe_subscription_id=stripe_subscription_id).first()
    if existing:
        for k, v in defaults.items():
            setattr(existing, k, v)
        try:
            existing.save(update_fields=list(defaults.keys()))
        except DatabaseError:
            # Row may have been deleted by a concurrent request; re-sync via update_or_create.
            sub, _ = WidgetSubscription.objects.update_or_create(
                stripe_subscription_id=stripe_subscription_id,
                defaults=defaults,
            )
        else:
            sub = existing
    else:
        # One row per business: find the row for this business (may not have stripe_subscription_id set yet).
        row = getattr(business, "widget_subscription", None)
        if row:
            for k, v in defaults.items():
                setattr(row, k, v)
            row.stripe_subscription_id = stripe_subscription_id
            row.save(update_fields=["stripe_subscription_id"] + list(defaults.keys()))
            sub = row
        else:
            sub, _ = WidgetSubscription.objects.update_or_create(
                business=business,
                defaults={**defaults, "stripe_subscription_id": stripe_subscription_id},
            )

    if not business.stripe_customer_id and stripe_customer_id:
        business.stripe_customer_id = stripe_customer_id
        business.save(update_fields=["stripe_customer_id"])

    logger.info(
        "subscription_sync: Widget sub_id=%s business_id=%s plan_id=%s status=%s",
        stripe_subscription_id, business.businessId, plan_id, status,
    )
    return sub, None


def sync_addon_subscription_from_stripe(stripe_subscription_id, subscription_obj=None, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING):
    """
    Sync a single Stripe subscription (addon) to BusinessAddonSubscription.
    Returns (BusinessAddonSubscription or None, error_message or None).
    """
    if not stripe_subscription_id:
        return None, "Missing stripe_subscription_id"
    try:
        if subscription_obj is None:
            subscription_obj = stripe.Subscription.retrieve(stripe_subscription_id)
    except stripe.StripeError as e:
        logger.warning("subscription_sync: Stripe retrieve addon failed sub_id=%s err=%s", stripe_subscription_id, e)
        return None, str(e)

    meta = _obj_get(subscription_obj, "metadata", {}) or {}
    business_id_str = meta.get("business_id")
    if not business_id_str:
        return None, "Missing business_id in subscription metadata"
    try:
        business = BusinessInfo.objects.get(businessId=int(business_id_str))
    except (BusinessInfo.DoesNotExist, ValueError):
        return None, "Business not found"

    status = (_obj_get(subscription_obj, "status", "") or "").strip().lower() or "incomplete"
    current_period_end = _subscription_to_period_end(subscription_obj)
    cancel_at_period_end = bool(_obj_get(subscription_obj, "cancel_at_period_end", False))

    items = _obj_get(_obj_get(subscription_obj, "items", {}) or {}, "data", []) or []
    stripe_price_id = None
    if items and items[0]:
        price = items[0].get("price") if isinstance(items[0], dict) else getattr(items[0], "price", None)
        if price is not None:
            stripe_price_id = (
                price if isinstance(price, str) else
                (price.get("id") if isinstance(price, dict) else getattr(price, "id", None))
            )

    customer_id = _obj_get(subscription_obj, "customer")
    stripe_customer_id = str(customer_id) if customer_id else ""

    defaults = {
        "business": business,
        "addon_type": addon_type,
        "stripe_customer_id": stripe_customer_id,
        "stripe_price_id": stripe_price_id,
        "status": status,
        "current_period_end": current_period_end,
        "cancel_at_period_end": cancel_at_period_end,
    }

    sub, _ = BusinessAddonSubscription.objects.update_or_create(
        stripe_subscription_id=stripe_subscription_id,
        defaults=defaults,
    )

    if addon_type == ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING:
        business.marketplace_email_branding_enabled = status in ("active", "trialing")
        business.save(update_fields=["marketplace_email_branding_enabled"])

    logger.info(
        "subscription_sync: Addon sub_id=%s business_id=%s addon_type=%s status=%s",
        stripe_subscription_id, business.businessId, addon_type, status,
    )
    return sub, None


def mark_widget_subscription_canceled(stripe_subscription_id):
    """Mark WidgetSubscription as canceled (e.g. on customer.subscription.deleted)."""
    updated = WidgetSubscription.objects.filter(stripe_subscription_id=stripe_subscription_id).update(status="canceled")
    if updated:
        logger.info("subscription_sync: Marked widget sub_id=%s as canceled", stripe_subscription_id)
    return updated


def mark_addon_subscription_canceled(stripe_subscription_id, addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING):
    """Mark BusinessAddonSubscription as canceled and disable addon on business."""
    sub = BusinessAddonSubscription.objects.filter(stripe_subscription_id=stripe_subscription_id).first()
    if not sub:
        return 0
    sub.status = "canceled"
    sub.save(update_fields=["status"])
    if addon_type == ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING and sub.business_id:
        BusinessInfo.objects.filter(pk=sub.business_id).update(marketplace_email_branding_enabled=False)
    logger.info("subscription_sync: Marked addon sub_id=%s as canceled", stripe_subscription_id)
    return 1
