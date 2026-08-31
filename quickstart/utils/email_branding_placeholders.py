"""
Build flat {{placeholder}} -> string maps from booking email context dicts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from django.conf import settings

from quickstart.utils.email_branding_constants import (
    EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED,
    EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST,
    EMAIL_TYPE_BOOKING_REMINDER,
    EMAIL_TYPE_BOOKING_RESCHEDULED,
)


def _fmt_long_date(d) -> str:
    if not d:
        return ""
    return d.strftime("%A, %B ") + str(d.day)


def _fmt_time_ampm(t) -> str:
    if not t:
        return ""
    # Mirror common email display (e.g. 7:30 PM)
    s = t.strftime("%I:%M %p")
    if s.startswith("0"):
        s = s[1:]
    return s


def _recipient_name_parts(user_like) -> tuple:
    if not user_like:
        return ("there", "")
    fn = getattr(user_like, "first_name", None) or ""
    ln = getattr(user_like, "last_name", None) or ""
    return (fn or "there", ln or "")


def _booking_time_range(booking) -> str:
    si = getattr(booking, "schedule_instance", None)
    if not si:
        return ""
    try:
        dummy_date = datetime.now().date()
        start_dt = datetime.combine(dummy_date, si.time)
        end_dt = start_dt + timedelta(minutes=si.duration or 0)
        return f"{_fmt_time_ampm(si.time)} - {_fmt_time_ampm(end_dt.time())}"
    except Exception:
        return str(si.time)


def _duration_str(booking) -> str:
    si = getattr(booking, "schedule_instance", None)
    if not si or not getattr(si, "duration", None):
        return ""
    d = si.duration
    return f"{d} min" if d else ""


def build_sample_placeholder_map(email_type_key: str) -> Dict[str, str]:
    """Dummy values for dashboard preview API."""
    base = {
        "class_title": "Sample Yoga Flow",
        "booking_date": "Monday, April 14",
        "booking_time": "7:00 PM",
        "time_range": "7:00 PM - 8:00 PM",
        "duration": "60 min",
        "location": "123 Studio Lane",
        "reference_id": "BK-12345",
        "business_name": "Sample Studio",
        "first_name": "Alex",
        "last_name": "Guest",
        "customer_email": "alex@example.com",
        "logo_url": "https://d1uuoquc68y10e.cloudfront.net/public/sig.png",
        "primary_color": "#f81e3e",
        "business_email": "hello@samplestudio.com",
        "business_phone": "+1 (555) 010-0000",
        "cancellation_policy": "Cancel up to 24 hours before for a full refund.",
        "participants": "2",
        "option_title": "Evening session",
        "equipment": "Bring a mat and water bottle.",
        "manage_booking_url": f"{settings.FRONTEND_BASE_URL}/my-classes?tab=upcoming",
        "cancel_booking_url": f"{settings.FRONTEND_BASE_URL}/?cancel_token=sample-token",
        "class_details_url": f"{settings.FRONTEND_BASE_URL}/classes/sample-class",
        "explore_url": f"{settings.FRONTEND_BASE_URL}/explore",
        "footer_text": "Questions? Reply to this email.",
        "confirmation_message": "We can't wait to see you!",
        "reason": "Instructor unavailable",
        "refund_info": "A full refund will appear in 5–10 business days.",
        "new_date": "Wednesday, April 16",
        "new_time": "6:00 PM - 7:00 PM",
        "timezone": "America / New York",
    }
    if email_type_key == EMAIL_TYPE_BOOKING_RESCHEDULED:
        base["booking_date"] = "Monday, April 14"
        base["booking_time"] = "7:00 PM"
    return base


def build_placeholder_map_from_context(email_type_key: str, context: Dict[str, Any]) -> Dict[str, str]:
    """
    Map template {{placeholders}} to string values from a send_* email context dict.
    """
    booking = context.get("booking")
    user = context.get("user")
    related = context.get("related_data") or {}
    first_name, last_name = _recipient_name_parts(user)
    email_addr = getattr(user, "email", None) or context.get("recipient_email") or ""

    si = getattr(booking, "schedule_instance", None) if booking else None
    booking_date = _fmt_long_date(si.date) if si else ""
    booking_time = _fmt_time_ampm(si.time) if si else ""
    time_range = context.get("formatted_time_range") or _booking_time_range(booking)
    duration = context.get("formatted_duration_minutes") or _duration_str(booking)

    ref = ""
    if booking:
        ref = str(
            getattr(booking, "user_facing_reference", None) or getattr(booking, "id", "") or ""
        )

    participants = ""
    if booking and getattr(booking, "participants", None) is not None:
        participants = str(booking.participants)

    branding = context.get("email_branding") or {}
    logo_url = (context.get("header_logo_url") or branding.get("logo_url") or "").strip()
    primary_color = (branding.get("primary_color") or "").strip() or "#f81e3e"
    footer_text = (branding.get("footer_text") or "").strip()
    confirmation_message = (branding.get("confirmation_message") or "").strip()

    m: Dict[str, str] = {
        "class_title": str(related.get("class_title") or ""),
        "booking_date": booking_date,
        "booking_time": booking_time,
        "time_range": str(time_range or ""),
        "duration": str(duration or ""),
        "location": str(related.get("class_location") or ""),
        "reference_id": ref,
        "business_name": str(related.get("business_name") or ""),
        "first_name": first_name,
        "last_name": last_name,
        "customer_email": str(email_addr or ""),
        "logo_url": logo_url,
        "primary_color": primary_color,
        "business_email": str(related.get("business_contact_email") or ""),
        "business_phone": str(related.get("business_contact_phone") or ""),
        "cancellation_policy": str(related.get("cancellation_policy_display") or ""),
        "participants": participants,
        "option_title": str(related.get("option_title") or ""),
        "equipment": str(related.get("equipment") or ""),
        "manage_booking_url": str(context.get("manage_bookings_url") or ""),
        "cancel_booking_url": str(context.get("guest_cancellation_url") or ""),
        "class_details_url": str(context.get("class_details_url") or ""),
        "explore_url": str(context.get("explore_url") or ""),
        "footer_text": footer_text,
        "confirmation_message": confirmation_message,
    }

    if email_type_key == EMAIL_TYPE_BOOKING_CANCELLED_BY_HOST:
        m["reason"] = str(context.get("reason") or "")
        amt = ""
        if booking and getattr(booking, "amount_paid", None) is not None:
            try:
                amt = f"${Decimal(str(booking.amount_paid)).quantize(Decimal('0.01'))} to your original payment method."
            except Exception:
                amt = str(booking.amount_paid)
        m["refund_info"] = amt or str(context.get("contact_info") or "")

    if email_type_key == EMAIL_TYPE_BOOKING_CANCELLATION_CONFIRMED:
        m["refund_info"] = str(context.get("refund_details") or "")

    if email_type_key == EMAIL_TYPE_BOOKING_RESCHEDULED:
        old_inst = context.get("old_instance")
        new_inst = context.get("new_instance")
        new_end = context.get("new_end_time")
        if old_inst:
            m["booking_date"] = _fmt_long_date(old_inst.date)
            m["booking_time"] = _fmt_time_ampm(old_inst.time)
        if new_inst:
            m["new_date"] = _fmt_long_date(new_inst.date)
            if new_end:
                m["new_time"] = f"{_fmt_time_ampm(new_inst.time)} - {_fmt_time_ampm(new_end)}"
            else:
                m["new_time"] = _fmt_time_ampm(new_inst.time)
        tz = context.get("formatted_timezone") or related.get("business_timezone") or ""
        m["timezone"] = str(tz).replace("_", " ") if tz else ""

    # Reminder uses calculated_end_time in template — enrich time_range if missing
    if email_type_key == EMAIL_TYPE_BOOKING_REMINDER and si:
        calc_end = context.get("calculated_end_time")
        if calc_end:
            m["time_range"] = f"{_fmt_time_ampm(si.time)} – {_fmt_time_ampm(calc_end)}"
        ftz = context.get("formatted_timezone") or related.get("business_timezone") or ""
        if ftz and not m.get("timezone"):
            m["timezone"] = str(ftz).replace("_", " ")

    return m
