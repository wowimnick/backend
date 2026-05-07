"""Emails for corporate shortlist & booking lifecycle (Resend / Django templates)."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from quickstart.models import CorporateBooking, CorporateShortlist
from quickstart.tasks import corporate_tasks as corporate_tasks_mod
from quickstart.tasks.corporate_tasks import _format_from

logger = logging.getLogger(__name__)


def _frontend():
    return getattr(settings, "FRONTEND_BASE_URL", "https://classeasily.com").rstrip("/")


def _send_html(to_list, subject, template_html, template_txt, ctx):
    if not to_list:
        return
    html = render_to_string(template_html, ctx)
    text = template_txt or ""
    msg = EmailMultiAlternatives(
        subject,
        text,
        _format_from(),
        to_list,
    )
    msg.attach_alternative(html, "text/html")
    msg.send(fail_silently=False)


@shared_task
def send_shortlist_to_corporate(shortlist_id: str):
    try:
        sl = CorporateShortlist.objects.select_related("inquiry").get(pk=shortlist_id)
    except CorporateShortlist.DoesNotExist:
        return
    inq = sl.inquiry
    url = f"{_frontend()}/corporate/shortlist/{sl.token}"
    ctx = {
        "shortlist": sl,
        "inquiry": inq,
        "settings": settings,
        "shortlist_url": url,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Your ClassEasily options for {inq.company_name}",
        "emails/corporate_shortlist_to_corp.html",
        f"View your shortlist: {url}\n",
        ctx,
    )


@shared_task
def send_shortlist_sent_to_admins(shortlist_id: str):
    try:
        sl = CorporateShortlist.objects.select_related("inquiry").get(pk=shortlist_id)
    except CorporateShortlist.DoesNotExist:
        return
    inq = sl.inquiry
    rec = corporate_tasks_mod._internal_recipients()
    if not rec:
        return
    url = f"{_frontend()}/corporate/shortlist/{sl.token}"
    ctx = {
        "shortlist": sl,
        "inquiry": inq,
        "settings": settings,
        "shortlist_url": url,
        "frontend_url": _frontend(),
    }
    _send_html(
        rec,
        f"[ClassEasily] Shortlist sent — {inq.company_name}",
        "emails/corporate_shortlist_to_admin.html",
        None,
        ctx,
    )


@shared_task
def send_option_selected_to_admins(booking_id: str):
    try:
        b = CorporateBooking.objects.select_related(
            "shortlist__inquiry", "selected_option"
        ).get(pk=booking_id)
    except CorporateBooking.DoesNotExist:
        return
    rec = corporate_tasks_mod._internal_recipients()
    if not rec:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "frontend_url": _frontend(),
    }
    _send_html(
        rec,
        f"[ClassEasily] {inq.company_name} selected an option — {b.reference}",
        "emails/corporate_option_selected_admin.html",
        None,
        ctx,
    )


@shared_task
def send_deposit_paid(booking_id: str):
    try:
        b = CorporateBooking.objects.select_related("shortlist__inquiry").get(
            pk=booking_id
        )
    except CorporateBooking.DoesNotExist:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Deposit received — {b.reference}",
        "emails/corporate_deposit_paid_corp.html",
        f"We received your deposit for {b.reference}.",
        ctx,
    )
    rec = corporate_tasks_mod._internal_recipients()
    if rec:
        _send_html(
            rec,
            f"[ClassEasily] Corporate deposit paid — {b.reference}",
            "emails/corporate_deposit_paid_admin.html",
            None,
            ctx,
        )


@shared_task
def send_invoice_issued(booking_id: str):
    try:
        b = CorporateBooking.objects.select_related("shortlist__inquiry").get(
            pk=booking_id
        )
    except CorporateBooking.DoesNotExist:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Invoice for your event balance — {b.reference}",
        "emails/corporate_invoice_issued_corp.html",
        f"Pay your balance: {b.invoice_url or 'check your email from Stripe'}",
        ctx,
    )
    rec = corporate_tasks_mod._internal_recipients()
    if rec:
        _send_html(
            rec,
            f"[ClassEasily] Balance invoice issued — {b.reference}",
            "emails/corporate_invoice_issued_admin.html",
            None,
            ctx,
        )


@shared_task
def send_balance_paid(booking_id: str):
    try:
        b = CorporateBooking.objects.select_related("shortlist__inquiry").get(
            pk=booking_id
        )
    except CorporateBooking.DoesNotExist:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Payment received — you are all set — {b.reference}",
        "emails/corporate_invoice_paid_corp.html",
        f"Your balance for {b.reference} is paid in full.",
        ctx,
    )
    rec = corporate_tasks_mod._internal_recipients()
    if rec:
        _send_html(
            rec,
            f"[ClassEasily] Corporate balance paid — {b.reference}",
            "emails/corporate_invoice_paid_admin.html",
            None,
            ctx,
        )


@shared_task
def send_booking_cancelled(booking_id: str):
    try:
        b = CorporateBooking.objects.select_related("shortlist__inquiry").get(
            pk=booking_id
        )
    except CorporateBooking.DoesNotExist:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Update on your ClassEasily booking — {b.reference}",
        "emails/corporate_booking_cancelled_corp.html",
        None,
        ctx,
    )
    rec = corporate_tasks_mod._internal_recipients()
    if rec:
        _send_html(
            rec,
            f"[ClassEasily] Corporate booking cancelled — {b.reference}",
            "emails/corporate_booking_cancelled_admin.html",
            None,
            ctx,
        )


@shared_task
def send_event_reminder(booking_id: str, days_before: int):
    try:
        b = CorporateBooking.objects.select_related("shortlist__inquiry").get(
            pk=booking_id
        )
    except CorporateBooking.DoesNotExist:
        return
    inq = b.shortlist.inquiry
    ctx = {
        "booking": b,
        "inquiry": inq,
        "settings": settings,
        "days_before": days_before,
        "frontend_url": _frontend(),
    }
    _send_html(
        [inq.email],
        f"Reminder: your team event is in {days_before} day(s) — {b.reference}",
        "emails/corporate_event_reminder_corp.html",
        None,
        ctx,
    )


@shared_task
def dispatch_corporate_event_reminders():
    """Daily: T-7 and T-1 reminders; idempotent via CorporateBookingEvent."""
    from quickstart.models import CorporateBookingEvent
    from quickstart.utils.corporate_events import log_corporate_booking_event

    now = timezone.now()
    today = now.date()
    for days in (7, 1):
        target = today + timedelta(days=days)
        qs = CorporateBooking.objects.filter(
            status__in=[
                CorporateBooking.ST_DEPOSIT_PAID,
                CorporateBooking.ST_INVOICED,
                CorporateBooking.ST_FULLY_PAID,
            ],
        )
        for b in qs:
            if not b.confirmed_datetime:
                continue
            if b.confirmed_datetime.date() != target:
                continue
            ev_type = f"reminder_tminus_{days}d_sent"
            if CorporateBookingEvent.objects.filter(
                booking=b, event_type=ev_type
            ).exists():
                continue
            try:
                send_event_reminder.delay(str(b.id), days)
            except Exception as e:
                logger.exception("reminder queue: %s", e)
            else:
                log_corporate_booking_event(
                    b, ev_type, f"Scheduled {days}-day reminder email."
                )
