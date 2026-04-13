"""Helpers for widget vs marketplace booking source strings on payments and emails."""

from django.utils import timezone

# Stripe / internal metadata values that count as widget-originated bookings
WIDGET_BOOKING_SOURCES = frozenset({"widget", "member_widget"})


def is_widget_booking_source(value) -> bool:
    if value is None:
        return False
    v = str(value).strip().lower()
    return v in WIDGET_BOOKING_SOURCES


def business_has_growth_or_advanced_widget_plan(business) -> bool:
    """
    True if business has an active Growth or Advanced widget subscription.

    Uses the same rules as WidgetSubscriptionView and _business_has_active_widget_subscription:
    active/trialing, and period not expired when current_period_end is set (NULL end is allowed).
    """
    if business is None:
        return False
    if getattr(business, "is_demo", False):
        return True
    from quickstart.services.widget_subscription_service import get_widget_subscription

    sub = get_widget_subscription(business)
    if not sub or not sub.plan_id:
        return False
    now = timezone.now()
    if (sub.status or "").strip().lower() not in ("active", "trialing"):
        return False
    if sub.current_period_end is not None and sub.current_period_end <= now:
        return False
    return (sub.plan_id or "").lower() in ("growth", "advanced")
