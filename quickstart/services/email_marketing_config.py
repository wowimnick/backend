"""
Stripe price_id -> email marketing tier limits and feature flags.
Set EMAIL_MARKETING_*_PRICE_ID env vars to your Stripe recurring Price IDs.
"""
from django.conf import settings

TIER_STARTER_KEY = "email_marketing_starter"
TIER_GROWTH_KEY = "email_marketing_growth"
TIER_BUSINESS_KEY = "email_marketing_business"
TIER_SCALE_KEY = "email_marketing_scale"


def _tier_defs():
    """Static ladder; Stripe Price IDs via env. ui_monthly_price is storefront MSRP (set Stripe to match)."""
    return [
        {
            "tier_key": TIER_STARTER_KEY,
            "plan_label": "Starter",
            "price_id": getattr(settings, "EMAIL_MARKETING_STARTER_PRICE_ID", None) or "",
            "annual_price_id": getattr(settings, "EMAIL_MARKETING_STARTER_PRICE_ID_ANNUAL", None) or "",
            "ui_monthly_price": 6,
            "monthly_marketing_send_limit": 2500,
            "max_recipients_per_campaign": 2500,
            "max_saved_templates": 5,
            "custom_domain_allowed": False,
            "raw_html_allowed": True,
            "advanced_segmentation": False,
            "scheduling_enabled": False,
            "saved_segments_enabled": False,
            "automation_enabled": False,
            "max_active_workflows": 0,
            "max_workflow_steps": 0,
        },
        {
            "tier_key": TIER_GROWTH_KEY,
            "plan_label": "Growth",
            "price_id": getattr(settings, "EMAIL_MARKETING_GROWTH_PRICE_ID", None) or "",
            "annual_price_id": getattr(settings, "EMAIL_MARKETING_GROWTH_PRICE_ID_ANNUAL", None) or "",
            "ui_monthly_price": 15,
            "monthly_marketing_send_limit": 10000,
            "max_recipients_per_campaign": 10000,
            "max_saved_templates": 25,
            "custom_domain_allowed": True,
            "raw_html_allowed": True,
            "advanced_segmentation": True,
            "scheduling_enabled": True,
            "saved_segments_enabled": True,
            "automation_enabled": False,
            "max_active_workflows": 0,
            "max_workflow_steps": 0,
        },
        {
            "tier_key": TIER_BUSINESS_KEY,
            "plan_label": "Business",
            "price_id": getattr(settings, "EMAIL_MARKETING_BUSINESS_PRICE_ID", None) or "",
            "annual_price_id": getattr(settings, "EMAIL_MARKETING_BUSINESS_PRICE_ID_ANNUAL", None) or "",
            "ui_monthly_price": 29,
            "monthly_marketing_send_limit": 50000,
            "max_recipients_per_campaign": 50000,
            "max_saved_templates": 75,
            "custom_domain_allowed": True,
            "raw_html_allowed": True,
            "advanced_segmentation": True,
            "scheduling_enabled": True,
            "saved_segments_enabled": True,
            "automation_enabled": True,
            "max_active_workflows": 5,
            "max_workflow_steps": 15,
        },
        {
            "tier_key": TIER_SCALE_KEY,
            "plan_label": "Scale",
            "price_id": getattr(settings, "EMAIL_MARKETING_SCALE_PRICE_ID", None) or "",
            "annual_price_id": getattr(settings, "EMAIL_MARKETING_SCALE_PRICE_ID_ANNUAL", None) or "",
            "ui_monthly_price": 59,
            "monthly_marketing_send_limit": 150000,
            "max_recipients_per_campaign": 150000,
            "max_saved_templates": 150,
            "custom_domain_allowed": True,
            "raw_html_allowed": True,
            "advanced_segmentation": True,
            "scheduling_enabled": True,
            "saved_segments_enabled": True,
            "automation_enabled": True,
            "max_active_workflows": 15,
            "max_workflow_steps": 40,
        },
    ]


def price_id_to_tier(price_id):
    if not price_id:
        return None
    for t in _tier_defs():
        if (t["price_id"] and t["price_id"] == price_id) or (
            t.get("annual_price_id") and t["annual_price_id"] == price_id
        ):
            return dict(t)
    return None


def list_public_tiers():
    """For billing UI: only tiers with a configured Stripe price."""
    out = []
    for t in _tier_defs():
        if not t.get("price_id"):
            continue
        out.append(
            {
                "tier_key": t["tier_key"],
                "plan_label": t.get("plan_label", t["tier_key"]),
                "price_id": t["price_id"],
                "ui_monthly_price": t.get("ui_monthly_price", 0),
                "monthly_marketing_send_limit": t["monthly_marketing_send_limit"],
                "max_recipients_per_campaign": t["max_recipients_per_campaign"],
                "max_saved_templates": t["max_saved_templates"],
                "custom_domain_allowed": t["custom_domain_allowed"],
                "raw_html_allowed": t["raw_html_allowed"],
                "advanced_segmentation": t.get("advanced_segmentation", True),
                "scheduling_enabled": t.get("scheduling_enabled", True),
                "saved_segments_enabled": t.get("saved_segments_enabled", True),
                "automation_enabled": t.get("automation_enabled", False),
                "max_active_workflows": t.get("max_active_workflows", 0),
                "max_workflow_steps": t.get("max_workflow_steps", 0),
            }
        )
    return out


def is_valid_marketing_price_id(price_id):
    return price_id_to_tier(price_id) is not None
