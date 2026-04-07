"""Helpers for widget vs marketplace booking source strings on payments and emails."""

from django.utils import timezone

from quickstart.models import WidgetSubscription

# Stripe / internal metadata values that count as widget-originated bookings
WIDGET_BOOKING_SOURCES = frozenset({"widget", "member_widget"})


def is_widget_booking_source(value) -> bool:
    if value is None:
        return False
    v = str(value).strip().lower()
    return v in WIDGET_BOOKING_SOURCES


def business_has_growth_or_advanced_widget_plan(business) -> bool:
    """True if business has an active Growth or Advanced widget subscription."""
    if business is None:
        return False
    if getattr(business, "is_demo", False):
        return True
    now = timezone.now()
    sub = (
        WidgetSubscription.objects.filter(
            business=business,
            status__in=["active", "trialing"],
            current_period_end__gt=now,
        )
        .order_by("-current_period_end")
        .first()
    )
    if not sub or not sub.plan_id:
        return False
    return (sub.plan_id or "").lower() in ("growth", "advanced")
