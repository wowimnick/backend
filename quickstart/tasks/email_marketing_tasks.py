"""Business marketing campaigns — increments quota only here (never transactional)."""
import html
import logging
import re

import resend
from celery import shared_task
from django.conf import settings
from django.core.signing import Signer
from django.utils import timezone

from quickstart.models import (
    ADDON_TYPE_EMAIL_MARKETING,
    BusinessAddonSubscription,
    BusinessEmailCampaign,
    CampaignEmailSend,
    Contact,
)
from quickstart.services.email_marketing_config import price_id_to_tier
from quickstart.services.marketing_audience import build_contact_queryset
from quickstart.services.marketing_builder import (
    marketing_body_contains_unsubscribe_merge_field,
    resolve_campaign_html_body,
)
from quickstart.services.email_marketing_usage import (
    MarketingQuotaExceeded,
    assert_can_send,
    increment_marketing_sent,
)
from quickstart.tasks.email_tasks import rate_limiter

logger = logging.getLogger(__name__)

# Resend allows up to 100 emails per /emails/batch request (fewer HTTP calls vs single-send).
RESEND_MARKETING_BATCH_SIZE = 100

_UNSUB_SIGNER = Signer(salt="ce-marketing-unsub-v1")


def build_unsubscribe_url(contact_id, business_id):
    token = _UNSUB_SIGNER.sign(f"{business_id}:{contact_id}")
    base = (getattr(settings, "DJANGO_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}/public/marketing-email/unsubscribe/{token}/"


def _merge_fields(html, contact, business):
    if not html:
        return ""
    fn = (contact.first_name or "").strip() if contact else ""
    ln = (contact.last_name or "").strip() if contact else ""
    cid = getattr(contact, "id", None) if contact else None
    unsub = (
        build_unsubscribe_url(str(cid), str(business.businessId))
        if cid and business
        else "#"
    )
    rep = {
        "{{first_name}}": fn,
        "{{last_name}}": ln,
        "{{business_name}}": getattr(business, "businessName", "") or "",
        "{{unsubscribe_url}}": unsub,
    }
    out = html
    for k, v in rep.items():
        out = out.replace(k, v)
    return out


def _footer_html(business, contact):
    """Physical address only; unsubscribe must appear in body via {{unsubscribe_url}}."""
    addr_raw = ""
    ms = getattr(business, "marketing_settings", None)
    if ms and ms.physical_address_footer:
        addr_raw = ms.physical_address_footer
    else:
        parts = [
            getattr(business, "businessAddress", "") or "",
            getattr(business, "businessCity", "") or "",
            getattr(business, "businessState", "") or "",
            getattr(business, "businessZipCode", "") or "",
        ]
        addr_raw = ", ".join(p for p in parts if p)
    addr_html = html.escape(addr_raw or "").replace("\n", "<br/>")
    align = "left"
    if ms:
        a = (getattr(ms, "footer_alignment", None) or "left").strip().lower()
        if a in ("center", "right"):
            align = a
    if not addr_html.strip():
        return ""
    return (
        f'<div style="text-align:{align};">'
        f'<hr style="border:none;border-top:1px solid #eee;margin:24px 0;" />'
        f'<p style="font-size:12px;color:#666;">{addr_html}</p>'
        f"</div>"
    )


def _resolve_from_header(business, profile, tier):
    default_from = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@classeasily.com")
    display = (profile.display_name if profile else None) or business.businessName
    if not tier.get("custom_domain_allowed"):
        return f"{display} <{default_from}>", default_from, business.studentContactEmail or None
    fe = (profile.from_email if profile else None) or default_from
    reply = profile.reply_to if profile and profile.reply_to else business.studentContactEmail
    return f"{display} <{fe}>", fe, reply


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={"max_retries": 3, "countdown": 20})
def send_business_marketing_campaign_task(self, campaign_id):
    try:
        campaign = BusinessEmailCampaign.objects.select_related(
            "business", "sender_profile"
        ).get(pk=campaign_id)
    except BusinessEmailCampaign.DoesNotExist:
        logger.error("send_business_marketing_campaign_task: campaign %s missing", campaign_id)
        return

    business = campaign.business
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
        campaign.status = "failed"
        campaign.error_message = "No active email marketing subscription."
        campaign.save(update_fields=["status", "error_message"])
        return

    tier = price_id_to_tier(addon.stripe_price_id)
    if not tier:
        campaign.status = "failed"
        campaign.error_message = "Unknown marketing plan."
        campaign.save(update_fields=["status", "error_message"])
        return

    at = (campaign.audience_type or "all_contacts").strip()
    flt = campaign.audience_filter or {}
    qs, _ = build_contact_queryset(business, at, flt)
    contacts = list(qs)
    try:
        assert_can_send(business, addon, len(contacts))
    except MarketingQuotaExceeded as e:
        campaign.status = "failed"
        campaign.error_message = str(e)
        campaign.save(update_fields=["status", "error_message"])
        return
    subject_base = campaign.subject or "Message from " + business.businessName
    html_base = resolve_campaign_html_body(campaign)
    if not marketing_body_contains_unsubscribe_merge_field(campaign):
        campaign.status = "failed"
        campaign.error_message = (
            "Your email must include the merge tag {{unsubscribe_url}} (for example in a link or "
            "button). Add it in the visual builder or HTML editor."
        )
        campaign.save(update_fields=["status", "error_message"])
        return

    from_header, from_email, reply_to = _resolve_from_header(
        business, campaign.sender_profile, tier
    )

    resend.api_key = settings.RESEND_API_KEY
    sent_ok = 0
    pending = []
    for contact in contacts:
        to = (contact.email or "").strip()
        if not to:
            continue
        to_norm = to.strip().lower()
        try:
            send_row, created = CampaignEmailSend.objects.get_or_create(
                campaign=campaign,
                to_email=to_norm,
                defaults={
                    "contact": contact,
                    "status": "queued",
                },
            )
        except Exception:
            continue
        if not created and send_row.status == "sent":
            continue

        body = _merge_fields(html_base, contact, business) + _footer_html(business, contact)
        pers_subject = _merge_fields(subject_base, contact, business)
        params = {
            "from": from_header,
            "to": [to],
            "subject": pers_subject[:1900],
            "html": body,
        }
        if reply_to:
            params["reply_to"] = reply_to
        pending.append((send_row, to, params))

    for batch_ix, chunk_start in enumerate(
        range(0, len(pending), RESEND_MARKETING_BATCH_SIZE)
    ):
        chunk = pending[chunk_start : chunk_start + RESEND_MARKETING_BATCH_SIZE]
        params_list = [p[2] for p in chunk]
        rate_limiter.wait_if_needed()
        try:
            result = resend.Batch.send(
                params_list,
                options={
                    "batch_validation": "permissive",
                    "idempotency_key": f"ce-mkt-{campaign_id}-{batch_ix}-{chunk_start}",
                },
            )
        except Exception as e:
            logger.exception(
                "Marketing batch send failed campaign=%s batch=%s: %s",
                campaign_id,
                batch_ix,
                e,
            )
            for send_row, _to, _params in chunk:
                send_row.status = "failed"
                send_row.save(update_fields=["status"])
            raise

        errors = result.get("errors") or []
        data = result.get("data") or []
        err_by_idx = {e["index"]: e for e in errors}
        data_cursor = 0
        now = timezone.now()
        for i, (send_row, _to, _params) in enumerate(chunk):
            if i in err_by_idx:
                send_row.status = "failed"
                send_row.save(update_fields=["status"])
                continue
            if data_cursor >= len(data):
                logger.error(
                    "Marketing batch response missing id campaign=%s batch=%s index=%s",
                    campaign_id,
                    batch_ix,
                    i,
                )
                send_row.status = "failed"
                send_row.save(update_fields=["status"])
                continue
            item = data[data_cursor]
            data_cursor += 1
            rid = item.get("id", "") if isinstance(item, dict) else getattr(item, "id", "")
            send_row.resend_email_id = rid or ""
            send_row.status = "sent"
            send_row.last_event_at = now
            send_row.save(
                update_fields=["resend_email_id", "status", "last_event_at"]
            )
            sent_ok += 1
        if data_cursor != len(data):
            logger.warning(
                "Marketing batch extra data entries campaign=%s batch=%s",
                campaign_id,
                batch_ix,
            )

    increment_marketing_sent(business, addon, sent_ok)
    campaign.status = "sent"
    campaign.sent_at = timezone.now()
    campaign.recipient_count = sent_ok
    campaign.scheduled_at = None
    campaign.save(
        update_fields=["status", "sent_at", "recipient_count", "scheduled_at"]
    )
    logger.info("send_business_marketing_campaign_task campaign=%s sent=%s", campaign_id, sent_ok)


@shared_task
def dispatch_due_scheduled_marketing_campaigns():
    """Beat task: move due scheduled campaigns to sending and queue Celery send."""
    from django.db import transaction

    now = timezone.now()
    due_ids = list(
        BusinessEmailCampaign.objects.filter(
            status="scheduled",
            scheduled_at__isnull=False,
            scheduled_at__lte=now,
        ).values_list("id", flat=True)[:40]
    )
    for cid in due_ids:
        with transaction.atomic():
            n = BusinessEmailCampaign.objects.filter(pk=cid, status="scheduled").update(
                status="sending",
                error_message="",
            )
        if n:
            send_business_marketing_campaign_task.delay(str(cid))
