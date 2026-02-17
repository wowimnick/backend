"""
Meta Conversions API (CAPI) - server-side events with deduplication.

Sends Purchase events to Meta so they can be deduplicated with Pixel events
using the same event_id (e.g. booking_id). See:
https://developers.facebook.com/docs/marketing-api/conversions-api/deduplicate-pixel-and-server-events/

Production only: CAPI is enabled only when DJANGO_ENV=prod (or META_CAPI_ENABLED=true).
Staging/local will not send even if META_PIXEL_ID and META_CAPI_ACCESS_TOKEN are set.

Testing (no impact on production dataset):
  - Set META_CAPI_TEST_EVENT_CODE to a code (e.g. TEST12345) and keep CAPI enabled.
  - Events are sent as test events: they appear in Events Manager > Test Events
    and are excluded from reporting/optimization.
  - In Events Manager: Data sources > your Pixel > Test Events; filter by your code.
"""

import hashlib
import logging
import os
import re
import time
from typing import Any, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Graph API version for CAPI
META_GRAPH_API_VERSION = "v21.0"
META_CAPI_EVENTS_URL = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{{pixel_id}}/events"


def log_capi_config_at_boot() -> None:
    """Log CAPI config status at Django boot. Safe: sensitive values show as Present/Not Present."""
    django_env = os.environ.get("DJANGO_ENV", "") or "Not set"
    enabled = getattr(settings, "META_CAPI_ENABLED", False)
    pixel_id = "Present" if (getattr(settings, "META_PIXEL_ID", "") or "").strip() else "Not Present"
    access_token = "Present" if (getattr(settings, "META_CAPI_ACCESS_TOKEN", "") or "").strip() else "Not Present"
    source_url = (getattr(settings, "META_CAPI_EVENT_SOURCE_URL", "") or "").strip() or "Not Present"
    test_code = (getattr(settings, "META_CAPI_TEST_EVENT_CODE", "") or "").strip() or "Not Present"

    if enabled and test_code != "Not Present":
        mode = "test"
    elif enabled:
        mode = "prod"
    else:
        mode = "disabled"

    logger.info(
        "Meta CAPI boot: DJANGO_ENV=%s, META_CAPI_ENABLED=%s, META_PIXEL_ID=%s, "
        "META_CAPI_ACCESS_TOKEN=%s, META_CAPI_EVENT_SOURCE_URL=%s, META_CAPI_TEST_EVENT_CODE=%s, mode=%s",
        django_env,
        enabled,
        pixel_id,
        access_token,
        source_url,
        test_code,
        mode,
    )


def _normalize_email(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return value.strip().lower() or None


def _normalize_phone(value: Optional[str], country_code: str = "1") -> Optional[str]:
    """Remove non-digits and leading zeros; prepend country code if missing."""
    if not value or not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value).lstrip("0") or "0"
    if not digits:
        return None
    # Meta expects country code (e.g. 1 for US/Canada). Default to 1 for North America.
    if not digits.startswith(country_code):
        digits = country_code + digits
    return digits or None


def _normalize_name(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return value.strip().lower() or None


def _normalize_city(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    # Lowercase, no punctuation, no spaces (replace spaces for city like "New York" -> "newyork")
    return re.sub(r"[^a-z0-9]", "", value.strip().lower()) or None


def _normalize_state(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return value.strip().lower()[:2] or None


def _normalize_zip(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return re.sub(r"[\s\-]", "", value.strip().lower())[:10] or None


def _normalize_country(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    return value.strip().lower()[:2] or None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_user_data(
    *,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    zip_code: Optional[str] = None,
    country: Optional[str] = None,
    client_ip_address: Optional[str] = None,
    client_user_agent: Optional[str] = None,
    country_code_phone: str = "1",
) -> dict[str, Any]:
    """
    Build CAPI user_data with normalized and SHA256-hashed PII.
    At least one hashed parameter is required; client_ip_address and client_user_agent are not hashed.
    """
    user_data: dict[str, Any] = {}

    if email:
        norm = _normalize_email(email)
        if norm:
            user_data["em"] = _sha256(norm)
    if phone:
        norm = _normalize_phone(phone, country_code_phone)
        if norm:
            user_data["ph"] = _sha256(norm)
    if first_name:
        norm = _normalize_name(first_name)
        if norm:
            user_data["fn"] = _sha256(norm)
    if last_name:
        norm = _normalize_name(last_name)
        if norm:
            user_data["ln"] = _sha256(norm)
    if city:
        norm = _normalize_city(city)
        if norm:
            user_data["ct"] = _sha256(norm)
    if state:
        norm = _normalize_state(state)
        if norm:
            user_data["st"] = _sha256(norm)
    if zip_code:
        norm = _normalize_zip(zip_code)
        if norm:
            user_data["zp"] = _sha256(norm)
    if country:
        norm = _normalize_country(country)
        if norm:
            user_data["country"] = _sha256(norm)
    else:
        user_data["country"] = _sha256("ca")

    if client_ip_address:
        user_data["client_ip_address"] = client_ip_address
    if client_user_agent:
        user_data["client_user_agent"] = client_user_agent

    return user_data


def send_purchase_event(
    *,
    event_id: str,
    value: float,
    currency: str = "CAD",
    content_ids: Optional[list] = None,
    content_type: str = "product",
    content_name: Optional[str] = None,
    num_items: int = 1,
    order_id: Optional[str] = None,
    event_source_url: Optional[str] = None,
    user_data: Optional[dict[str, Any]] = None,
    client_ip_address: Optional[str] = None,
    client_user_agent: Optional[str] = None,
) -> bool:
    """
    Send a Purchase event to Meta Conversions API with deduplication.

    Use the same event_id (e.g. booking_id) in both Pixel (eventID) and CAPI (event_id)
    so Meta can deduplicate. See:
    https://developers.facebook.com/docs/marketing-api/conversions-api/deduplicate-pixel-and-server-events/

    Returns True if the event was sent successfully, False otherwise (missing config or API error).
    """
    # Only send in production (staging/local will not send even if tokens are set)
    if not getattr(settings, "META_CAPI_ENABLED", False):
        logger.debug("Meta CAPI skipped: not enabled (prod only).")
        return False

    pixel_id = getattr(settings, "META_PIXEL_ID", None)
    access_token = getattr(settings, "META_CAPI_ACCESS_TOKEN", None)
    if not pixel_id or not access_token:
        logger.debug(
            "Meta CAPI skipped: META_PIXEL_ID or META_CAPI_ACCESS_TOKEN not set."
        )
        return False

    if not user_data:
        logger.warning("Meta CAPI Purchase skipped: no user_data provided.")
        return False

    # event_source_url is recommended for website events
    if not event_source_url:
        event_source_url = getattr(
            settings, "META_CAPI_EVENT_SOURCE_URL", None
        ) or (getattr(settings, "FRONTEND_BASE_URL", None) or "https://www.classeasily.com")

    event_time = int(time.time())
    custom_data = {
        "currency": currency,
        "value": round(float(value), 2),
        "content_type": content_type,
        "num_items": num_items,
    }
    if content_ids is not None:
        custom_data["content_ids"] = [str(x) for x in content_ids]
    if content_name:
        custom_data["content_name"] = content_name
    if order_id:
        custom_data["order_id"] = str(order_id)

    if client_ip_address:
        user_data = {**user_data, "client_ip_address": client_ip_address}
    if client_user_agent:
        user_data = {**user_data, "client_user_agent": client_user_agent}

    payload = {
        "data": [
            {
                "event_name": "Purchase",
                "event_time": event_time,
                "event_id": str(event_id),
                "action_source": "website",
                "user_data": user_data,
                "custom_data": custom_data,
                "event_source_url": event_source_url,
            }
        ]
    }
    # Test events: do not affect reporting; view in Events Manager > Test Events
    test_event_code = getattr(settings, "META_CAPI_TEST_EVENT_CODE", None) or ""
    if test_event_code:
        payload["test_event_code"] = test_event_code.strip()

    url = META_CAPI_EVENTS_URL.format(pixel_id=pixel_id)
    params = {"access_token": access_token}

    try:
        resp = requests.post(url, json=payload, params=params, timeout=10)
        if resp.ok:
            logger.info(
                "Meta CAPI Purchase sent successfully (event_id=%s).",
                event_id,
            )
            return True
        logger.warning(
            "Meta CAPI request failed: status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return False
    except requests.RequestException as e:
        logger.warning("Meta CAPI request error: %s", e, exc_info=True)
        return False


def _user_data_from_booking(booking, request=None) -> Optional[dict[str, Any]]:
    """Build CAPI user_data from a Booking's user or contact."""
    email = None
    phone = None
    first_name = None
    last_name = None
    city = None
    state = None
    zip_code = None
    country = "ca"

    if booking.user:
        u = booking.user
        email = getattr(u, "email", None)
        first_name = getattr(u, "first_name", None) or getattr(u, "firstName", None)
        last_name = getattr(u, "last_name", None) or getattr(u, "lastName", None)
        if hasattr(u, "phone_number"):
            phone = getattr(u, "phone_number", None)
    if booking.contact:
        c = booking.contact
        if not email:
            email = getattr(c, "email", None)
        if not phone:
            phone = getattr(c, "phone_number", None)
        if not first_name:
            first_name = getattr(c, "first_name", None)
        if not last_name:
            last_name = getattr(c, "last_name", None)

    user_data = _build_user_data(
        email=email,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
        country_code_phone="1",
    )
    if not user_data:
        return None

    if request:
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        client_ip = (xff.split(",")[0].strip() if xff else None) or request.META.get(
            "REMOTE_ADDR"
        )
        if client_ip:
            user_data["client_ip_address"] = client_ip
        ua = request.META.get("HTTP_USER_AGENT")
        if ua:
            user_data["client_user_agent"] = ua

        # Meta Parameter Builder: fbc and fbp improve matching (do not normalize; _fbc is case-sensitive).
        # Prefer cookies (same-origin); fallback to body when frontend sends meta_fbc/meta_fbp (e.g. cross-origin).
        fbc = None
        fbp = None
        if hasattr(request, "COOKIES"):
            fbc = request.COOKIES.get("_fbc")
            fbp = request.COOKIES.get("_fbp")
        if not fbc and getattr(request, "data", None) and isinstance(request.data, dict):
            fbc = request.data.get("meta_fbc") or None
        if not fbp and getattr(request, "data", None) and isinstance(request.data, dict):
            fbp = request.data.get("meta_fbp") or None
        if fbc:
            user_data["fbc"] = fbc
        if fbp:
            user_data["fbp"] = fbp

    return user_data


def _class_info_from_booking(booking) -> tuple[Optional[list], Optional[str]]:
    """Get (content_ids, content_name) from booking's schedule_instance -> option -> classId."""
    try:
        si = booking.schedule_instance
        if not si:
            return None, None
        schedule = getattr(si, "schedule", None)
        if not schedule:
            return None, None
        option = getattr(schedule, "option", None)
        if not option:
            return None, None
        class_obj = getattr(option, "classId", None)
        if not class_obj:
            return None, None
        class_id = getattr(class_obj, "classId", None) or getattr(class_obj, "pk", None)
        title = getattr(class_obj, "title", None)
        content_ids = [str(class_id)] if class_id is not None else None
        return content_ids, title
    except Exception as e:
        logger.debug("Could not get class info from booking for CAPI: %s", e)
        return None, None


def send_purchase_event_for_booking(
    booking,
    value,
    currency: str = "CAD",
    content_ids: Optional[list] = None,
    content_name: Optional[str] = None,
    num_items: int = 1,
    event_source_url: Optional[str] = None,
    request=None,
    meta_fbc: Optional[str] = None,
    meta_fbp: Optional[str] = None,
) -> bool:
    """
    Send a CAPI Purchase event for a confirmed booking, with deduplication.

    Uses booking.id as event_id so the frontend Pixel can send the same eventID
    (booking_id) for deduplication. Builds user_data from booking.user or booking.contact.
    If content_ids/content_name are not provided, tries to derive from booking's class.

    When request is None (e.g. Stripe webhook), pass meta_fbc/meta_fbp from
    PaymentIntent metadata so CAPI still gets fbc/fbp for paid conversions.
    """
    user_data = _user_data_from_booking(booking, request=request)
    if not user_data:
        logger.debug(
            "Meta CAPI Purchase skipped for booking %s: no user/contact data.",
            booking.id,
        )
        return False

    # Paid conversions (webhook): merge fbc/fbp from PaymentIntent metadata when no request
    if meta_fbc:
        user_data["fbc"] = meta_fbc
    if meta_fbp:
        user_data["fbp"] = meta_fbp

    if content_ids is None and content_name is None:
        content_ids, content_name = _class_info_from_booking(booking)

    return send_purchase_event(
        event_id=str(booking.id),
        value=float(value),
        currency=currency,
        content_ids=content_ids or [],
        content_type="product",
        content_name=content_name,
        num_items=num_items,
        order_id=str(booking.id),
        event_source_url=event_source_url,
        user_data=user_data,
    )
