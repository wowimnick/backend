"""One-way push of bookings to Google Calendar / Microsoft Outlook."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytz
from django.conf import settings
from django.utils import timezone

from quickstart.models import CalendarConnection

logger = logging.getLogger(__name__)


def _session_datetimes(booking):
    instance = booking.schedule_instance
    if not instance:
        return None, None
    business = instance.schedule.option.classId.businessId
    tz = pytz.timezone(business.business_timezone or "America/Toronto")
    start = tz.localize(datetime.combine(instance.date, instance.time))
    end = start + timedelta(minutes=int(instance.duration or 60))
    return start, end


def _event_payload(booking):
    instance = booking.schedule_instance
    service = instance.schedule.option.classId
    start, end = _session_datetimes(booking)
    client = ""
    if booking.contact:
        client = f"{booking.contact.first_name} {booking.contact.last_name}".strip()
    elif booking.user:
        client = f"{booking.user.first_name} {booking.user.last_name}".strip() or booking.user.email
    return {
        "summary": f"{service.title} — {client or 'Client'}",
        "description": f"Booking {booking.user_facing_reference or booking.id}",
        "start": start,
        "end": end,
    }


def _google_headers(connection):
    token = _ensure_google_token(connection)
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _ensure_google_token(connection):
    if connection.access_token and (
        not connection.token_expires_at
        or connection.token_expires_at > timezone.now() + timedelta(minutes=2)
    ):
        return connection.access_token
    client_id = getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") or getattr(
        settings, "GOOGLE_OAUTH_CLIENT_ID", ""
    )
    client_secret = getattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", "") or getattr(
        settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""
    )
    if not client_id or not client_secret or not connection.refresh_token:
        return connection.access_token or None
    try:
        import requests

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": connection.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        connection.access_token = data.get("access_token") or connection.access_token
        expires_in = int(data.get("expires_in") or 3600)
        connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        connection.last_error = ""
        connection.save(
            update_fields=["access_token", "token_expires_at", "last_error", "updated_at"]
        )
        return connection.access_token
    except Exception as exc:
        logger.warning("Google token refresh failed for business %s: %s", connection.business_id, exc)
        connection.last_error = str(exc)[:500]
        connection.save(update_fields=["last_error", "updated_at"])
        return None


def _push_google(connection, booking, action):
    import requests

    headers = _google_headers(connection)
    if not headers:
        raise RuntimeError("Google Calendar is not connected.")
    calendar_id = connection.calendar_id or "primary"
    payload = _event_payload(booking)
    body = {
        "summary": payload["summary"],
        "description": payload["description"],
        "start": {"dateTime": payload["start"].isoformat(), "timeZone": str(payload["start"].tzinfo)},
        "end": {"dateTime": payload["end"].isoformat(), "timeZone": str(payload["end"].tzinfo)},
    }
    if action == "delete" and booking.calendar_event_id:
        url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{booking.calendar_event_id}"
        requests.delete(url, headers=headers, timeout=15)
        return ""
    if booking.calendar_event_id and action in ("update", "create"):
        url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{booking.calendar_event_id}"
        resp = requests.patch(url, headers=headers, json=body, timeout=15)
        if resp.status_code == 404:
            booking.calendar_event_id = ""
        else:
            resp.raise_for_status()
            return resp.json().get("id") or booking.calendar_event_id
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("id") or ""


def _ensure_outlook_token(connection):
    if connection.access_token and (
        not connection.token_expires_at
        or connection.token_expires_at > timezone.now() + timedelta(minutes=2)
    ):
        return connection.access_token
    client_id = getattr(settings, "MICROSOFT_CALENDAR_CLIENT_ID", "") or getattr(
        settings, "MS_GRAPH_CLIENT_ID", ""
    )
    client_secret = getattr(settings, "MICROSOFT_CALENDAR_CLIENT_SECRET", "") or getattr(
        settings, "MS_GRAPH_CLIENT_SECRET", ""
    )
    if not client_id or not client_secret or not connection.refresh_token:
        return connection.access_token or None
    try:
        import requests

        resp = requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": connection.refresh_token,
                "grant_type": "refresh_token",
                "scope": "offline_access Calendars.ReadWrite",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        connection.access_token = data.get("access_token") or connection.access_token
        if data.get("refresh_token"):
            connection.refresh_token = data["refresh_token"]
        expires_in = int(data.get("expires_in") or 3600)
        connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
        connection.last_error = ""
        connection.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "token_expires_at",
                "last_error",
                "updated_at",
            ]
        )
        return connection.access_token
    except Exception as exc:
        logger.warning("Outlook token refresh failed for business %s: %s", connection.business_id, exc)
        connection.last_error = str(exc)[:500]
        connection.save(update_fields=["last_error", "updated_at"])
        return None


def _push_outlook(connection, booking, action):
    import requests

    token = _ensure_outlook_token(connection)
    if not token:
        raise RuntimeError("Outlook Calendar is not connected.")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = _event_payload(booking)
    body = {
        "subject": payload["summary"],
        "body": {"contentType": "text", "content": payload["description"]},
        "start": {
            "dateTime": payload["start"].strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": str(payload["start"].tzinfo),
        },
        "end": {
            "dateTime": payload["end"].strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": str(payload["end"].tzinfo),
        },
    }
    calendar_segment = (
        f"/me/calendars/{connection.calendar_id}/events"
        if connection.calendar_id
        else "/me/events"
    )
    base = "https://graph.microsoft.com/v1.0"
    if action == "delete" and booking.calendar_event_id:
        requests.delete(
            f"{base}{calendar_segment}/{booking.calendar_event_id}".replace("/events/", "/events/"),
            headers=headers,
            timeout=15,
        )
        return ""
    if booking.calendar_event_id:
        resp = requests.patch(
            f"{base}/me/events/{booking.calendar_event_id}",
            headers=headers,
            json=body,
            timeout=15,
        )
        if resp.status_code != 404:
            resp.raise_for_status()
            return booking.calendar_event_id
    resp = requests.post(f"{base}{calendar_segment}", headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("id") or ""


def push_booking_to_calendars(booking, action="create"):
    """Push create/update/delete. Failures are recorded; they never raise to the caller."""
    instance = booking.schedule_instance
    if not instance:
        return
    business = instance.schedule.option.classId.businessId
    connections = CalendarConnection.objects.filter(business=business, is_active=True)
    event_id = booking.calendar_event_id
    for connection in connections:
        try:
            if connection.provider == "google":
                event_id = _push_google(connection, booking, action) or event_id
            elif connection.provider == "outlook":
                event_id = _push_outlook(connection, booking, action) or event_id
            connection.last_synced_at = timezone.now()
            connection.last_error = ""
            connection.save(update_fields=["last_synced_at", "last_error", "updated_at"])
        except Exception as exc:
            logger.warning(
                "Calendar push failed (%s) for booking %s: %s",
                connection.provider,
                booking.id,
                exc,
            )
            connection.last_error = str(exc)[:500]
            connection.save(update_fields=["last_error", "updated_at"])
    if event_id != booking.calendar_event_id:
        booking.calendar_event_id = event_id or ""
        booking.save(update_fields=["calendar_event_id"])
