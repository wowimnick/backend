"""Process marketing workflow enrollments (delay + send_email steps)."""
import logging
from datetime import timedelta

import resend
from celery import shared_task
from django.conf import settings
from django.db.utils import OperationalError
from django.utils import timezone

from quickstart.models import (
    ADDON_TYPE_EMAIL_MARKETING,
    BusinessAddonSubscription,
    MarketingSenderProfile,
    MarketingWorkflowEnrollment,
)
from quickstart.services.email_marketing_config import price_id_to_tier
from quickstart.services.marketing_builder import resolve_campaign_html_body
from quickstart.services.email_marketing_usage import (
    MarketingQuotaExceeded,
    assert_can_send,
    increment_marketing_sent,
)
from quickstart.tasks.email_marketing_tasks import (
    _footer_html,
    _merge_fields,
    _resolve_from_header,
)
from quickstart.tasks.email_tasks import rate_limiter
from quickstart.utils.marketing_html import sanitize_marketing_html

logger = logging.getLogger(__name__)


class _BodyProxy:
    def __init__(self, cfg):
        self.content_type = (cfg.get("content_type") or "html").strip()
        self.html_body = cfg.get("html_body") or ""
        self.builder_json = cfg.get("builder_json") if isinstance(cfg.get("builder_json"), dict) else {}


def _send_workflow_email(*, business, addon, tier, contact, cfg, sender_profile_id=None):
    subject = (cfg.get("subject") or "Message").strip()
    if not subject:
        return False, "Missing subject"
    body_html = sanitize_marketing_html(resolve_campaign_html_body(_BodyProxy(cfg)))
    if not body_html.strip():
        return False, "Missing body"
    try:
        assert_can_send(business, addon, 1)
    except MarketingQuotaExceeded as e:
        return False, str(e)

    profile = None
    if sender_profile_id:
        profile = MarketingSenderProfile.objects.filter(
            id=sender_profile_id, business=business
        ).first()

    subject_m = _merge_fields(subject, contact, business)
    body_m = _merge_fields(body_html, contact, business) + _footer_html(business, contact)
    from_header, _fe, reply_to = _resolve_from_header(business, profile, tier)
    to = (contact.email or "").strip()
    if not to:
        return False, "No email"

    resend.api_key = settings.RESEND_API_KEY
    try:
        rate_limiter.wait_if_needed()
        params = {
            "from": from_header,
            "to": [to],
            "subject": subject_m[:1900],
            "html": body_m,
        }
        if reply_to:
            params["reply_to"] = reply_to
        resend.Emails.send(params)
    except Exception as e:
        logger.exception("workflow email: %s", e)
        return False, str(e)

    increment_marketing_sent(business, addon, 1)
    return True, None


@shared_task(
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def process_due_workflow_enrollments():
    now = timezone.now()
    qs = (
        MarketingWorkflowEnrollment.objects.filter(
            status="active",
            next_run_at__lte=now,
        )
        .select_related("workflow", "contact", "workflow__business")
        .order_by("next_run_at")[:35]
    )
    for en in qs:
        try:
            _run_enrollment_once(en)
        except Exception as e:
            logger.exception("enrollment %s: %s", en.id, e)


def _run_enrollment_once(en: MarketingWorkflowEnrollment):
    wf = en.workflow
    business = wf.business
    contact = en.contact
    if wf.status != "active":
        en.status = "cancelled"
        en.next_run_at = None
        en.save(update_fields=["status", "next_run_at", "updated_at"])
        return
    if not contact or not (contact.email or "").strip() or contact.marketing_unsubscribed:
        en.status = "cancelled"
        en.next_run_at = None
        en.save(update_fields=["status", "next_run_at", "updated_at"])
        return

    addon = (
        BusinessAddonSubscription.objects.filter(
            business=business,
            addon_type=ADDON_TYPE_EMAIL_MARKETING,
            status__in=["active", "trialing"],
        )
        .order_by("-current_period_end")
        .first()
    )
    if not addon:
        en.status = "cancelled"
        en.next_run_at = None
        en.save(update_fields=["status", "next_run_at", "updated_at"])
        return
    tier = price_id_to_tier(addon.stripe_price_id)
    if not tier or not tier.get("automation_enabled"):
        en.status = "cancelled"
        en.next_run_at = None
        en.save(update_fields=["status", "next_run_at", "updated_at"])
        return

    steps = list(wf.steps.all().order_by("order"))
    idx = en.current_step_index
    if idx >= len(steps):
        en.status = "completed"
        en.next_run_at = None
        en.save(update_fields=["status", "next_run_at", "updated_at"])
        return

    step = steps[idx]
    if step.step_type == "delay":
        hours = float(step.config.get("hours", 24))
        en.next_run_at = timezone.now() + timedelta(hours=hours)
        en.current_step_index = idx + 1
        en.save(update_fields=["next_run_at", "current_step_index", "updated_at"])
        return

    if step.step_type == "send_email":
        cfg = step.config if isinstance(step.config, dict) else {}
        sp = cfg.get("sender_profile")
        ok, err = _send_workflow_email(
            business=business,
            addon=addon,
            tier=tier,
            contact=contact,
            cfg=cfg,
            sender_profile_id=str(sp) if sp else None,
        )
        if not ok:
            logger.warning("workflow send failed enrollment=%s: %s", en.id, err)
        en.current_step_index = idx + 1
        en.next_run_at = timezone.now()
        en.save(update_fields=["current_step_index", "next_run_at", "updated_at"])
        return

    # Unknown step — skip
    en.current_step_index = idx + 1
    en.next_run_at = timezone.now()
    en.save(update_fields=["current_step_index", "next_run_at", "updated_at"])
