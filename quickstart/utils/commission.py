"""Canonical SaaS commission resolver. Plan tiers only; PartnerTier is deprecated."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

WIDGET_PLAN_FEE_PERCENT = {
    "basic": Decimal("4.00"),
    "growth": Decimal("3.00"),
    "advanced": Decimal("2.00"),
}


def get_plan_fee_percentage(business, plan_id=None):
    """Return the ClassEasily commission percent for a business (0 if waived)."""
    waived_until = getattr(business, "commission_waived_until", None)
    if waived_until and timezone.now() < waived_until:
        return Decimal("0.00")

    if plan_id:
        return WIDGET_PLAN_FEE_PERCENT.get(str(plan_id).lower(), Decimal("4.00"))

    sub = getattr(business, "widget_subscription", None)
    if sub is None:
        try:
            sub = business.widget_subscription
        except Exception:
            sub = None

    if sub and getattr(sub, "plan_id", None):
        return WIDGET_PLAN_FEE_PERCENT.get(
            str(sub.plan_id).lower(), Decimal("4.00")
        )
    return Decimal("4.00")


def ensure_commission_waiver(business, months=3):
    """Set first-3-months waiver if the business does not already have one."""
    if getattr(business, "commission_waived_until", None):
        return business.commission_waived_until
    until = timezone.now() + timedelta(days=30 * months)
    business.commission_waived_until = until
    business.save(update_fields=["commission_waived_until"])
    return until
