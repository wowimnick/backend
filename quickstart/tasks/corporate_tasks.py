import logging

from celery import shared_task
from django.conf import settings
from django.template.loader import render_to_string

from quickstart.utils.email_utils import get_super_admin_emails

logger = logging.getLogger(__name__)


def _format_from():
    name = (settings.NOTIFICATION_SETTINGS or {}).get("default_from_name", "ClassEasily")
    email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@classeasily.com")
    return f"{name} <{email}>"


def _leads_recipients():
    raw = (getattr(settings, "CORPORATE_LEADS_EMAIL", None) or "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _super_admin_recipients():
    return list(get_super_admin_emails())


def _internal_recipients():
    """All Super Admins plus optional CORPORATE_LEADS_EMAIL list, deduped (case-insensitive)."""
    seen = set()
    out = []
    for email in _super_admin_recipients() + _leads_recipients():
        key = (email or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(email.strip())
    return out


def queue_corporate_inquiry_emails(inquiry):
    """
    Queue internal + confirmation emails using send_transactional_email_task — the same
    Celery task and Resend path as the rest of the app (booking, auth, etc.).

    Previously we used a dedicated @shared_task that called EmailMultiAlternatives.send()
    inside the worker; that path did not always run (task registration / visibility) and
    differed from other mail. Rendering happens here so the worker only runs the standard
    transactional sender.
    """
    from quickstart.tasks.email_tasks import send_transactional_email_task

    ctx = {
        "inquiry": inquiry,
        "settings": settings,
        "frontend_url": getattr(settings, "FRONTEND_BASE_URL", "https://classeasily.com").rstrip(
            "/"
        ),
    }
    from_email = _format_from()

    recipients = _internal_recipients()
    if recipients:
        subject = f"[ClassEasily Corporate] New inquiry: {inquiry.company_name}"
        html = render_to_string("emails/corporate_inquiry_internal.html", ctx)
        text = (
            f"New corporate inquiry\n\n"
            f"Company: {inquiry.company_name}\n"
            f"Contact: {inquiry.contact_name}\n"
            f"Email: {inquiry.email}\n"
            f"Phone: {inquiry.phone or '—'}\n"
            f"Company size: {inquiry.get_company_size_display()}\n\n"
            f"Message:\n{inquiry.message or '—'}\n"
        )
        try:
            async_result = send_transactional_email_task.delay(
                subject=subject,
                html=html,
                text=text,
                to=recipients,
                from_email=from_email,
                reply_to=[inquiry.email],
            )
            logger.info(
                "Queued corporate inquiry internal email (inquiry_id=%s, celery_task_id=%s)",
                inquiry.pk,
                async_result.id,
            )
        except Exception as exc:
            logger.exception("Failed to queue corporate inquiry internal email: %s", exc)
            raise
    else:
        logger.warning(
            "No Super Admin emails and CORPORATE_LEADS_EMAIL empty; skipping internal "
            "corporate inquiry email (id=%s)",
            inquiry.pk,
        )

    subj = "We received your ClassEasily corporate inquiry"
    html = render_to_string("emails/corporate_inquiry_confirmation.html", ctx)
    text = (
        f"Hi {inquiry.contact_name},\n\n"
        f"Thanks for reaching out about team experiences for {inquiry.company_name}. "
        f"Our team will review your note and reply shortly.\n\n"
        f"— ClassEasily\n"
    )
    try:
        async_result = send_transactional_email_task.delay(
            subject=subj,
            html=html,
            text=text,
            to=[inquiry.email],
            from_email=from_email,
        )
        logger.info(
            "Queued corporate inquiry confirmation (inquiry_id=%s, celery_task_id=%s)",
            inquiry.pk,
            async_result.id,
        )
    except Exception as exc:
        logger.exception("Failed to queue corporate inquiry confirmation email: %s", exc)
        raise


@shared_task
def send_corporate_inquiry_emails(inquiry_id: str):
    """Legacy Celery entrypoint; loads inquiry and delegates to queue_corporate_inquiry_emails."""
    from quickstart.models import CorporateInquiry

    try:
        inquiry = CorporateInquiry.objects.get(pk=inquiry_id)
    except CorporateInquiry.DoesNotExist:
        logger.warning("CorporateInquiry not found: %s", inquiry_id)
        return
    queue_corporate_inquiry_emails(inquiry)
