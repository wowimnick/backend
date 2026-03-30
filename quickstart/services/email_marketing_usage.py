"""Marketing send quota (transactional emails never use this)."""
from django.db import transaction
from django.db.models import F

from quickstart.models import BusinessMarketingUsage
from quickstart.services.email_marketing_config import price_id_to_tier


class MarketingQuotaExceeded(Exception):
    def __init__(self, message="Marketing email quota exceeded for this billing period.", used=0, limit=0):
        super().__init__(message)
        self.used = used
        self.limit = limit


def get_usage_row(business, period_start, period_end):
    if not period_start or not period_end:
        return None
    row, _ = BusinessMarketingUsage.objects.get_or_create(
        business=business,
        period_start=period_start,
        period_end=period_end,
        defaults={"marketing_emails_sent": 0},
    )
    return row


def usage_snapshot(business, addon_sub):
    """Returns dict for API or None if no tier."""
    if not addon_sub:
        return None
    tier = price_id_to_tier(addon_sub.stripe_price_id)
    if not tier:
        return None
    row = get_usage_row(business, addon_sub.current_period_start, addon_sub.current_period_end)
    used = row.marketing_emails_sent if row else 0
    return {
        "tier_key": tier["tier_key"],
        "monthly_limit": tier["monthly_marketing_send_limit"],
        "used_this_period": used,
        "remaining": max(0, tier["monthly_marketing_send_limit"] - used),
        "period_start": addon_sub.current_period_start,
        "period_end": addon_sub.current_period_end,
        "max_recipients_per_campaign": tier["max_recipients_per_campaign"],
        "max_saved_templates": tier["max_saved_templates"],
        "custom_domain_allowed": tier["custom_domain_allowed"],
        "raw_html_allowed": tier["raw_html_allowed"],
        "advanced_segmentation": tier.get("advanced_segmentation", True),
        "scheduling_enabled": tier.get("scheduling_enabled", True),
        "saved_segments_enabled": tier.get("saved_segments_enabled", True),
        "automation_enabled": tier.get("automation_enabled", False),
        "max_active_workflows": tier.get("max_active_workflows", 0),
        "max_workflow_steps": tier.get("max_workflow_steps", 0),
    }


def assert_can_send(business, addon_sub, recipient_count):
    tier = price_id_to_tier(addon_sub.stripe_price_id)
    if not tier:
        raise MarketingQuotaExceeded("Unknown email marketing plan.", 0, 0)
    if recipient_count > tier["max_recipients_per_campaign"]:
        raise MarketingQuotaExceeded(
            f"This campaign exceeds your per-campaign limit ({tier['max_recipients_per_campaign']}).",
            0,
            tier["max_recipients_per_campaign"],
        )
    with transaction.atomic():
        row = get_usage_row(business, addon_sub.current_period_start, addon_sub.current_period_end)
        if row is None:
            raise MarketingQuotaExceeded("Billing period not ready; try again shortly.", 0, 0)
        locked = BusinessMarketingUsage.objects.select_for_update().filter(pk=row.pk).first()
        if locked.marketing_emails_sent + recipient_count > tier["monthly_marketing_send_limit"]:
            raise MarketingQuotaExceeded(
                "Not enough marketing emails left this billing period.",
                locked.marketing_emails_sent,
                tier["monthly_marketing_send_limit"],
            )
    return tier


def increment_marketing_sent(business, addon_sub, delta):
    """Call after successful marketing sends only (not transactional)."""
    if delta <= 0:
        return
    row = get_usage_row(business, addon_sub.current_period_start, addon_sub.current_period_end)
    if row is None:
        return
    BusinessMarketingUsage.objects.filter(pk=row.pk).update(
        marketing_emails_sent=F("marketing_emails_sent") + delta
    )
