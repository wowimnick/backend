"""Shared audience queryset builder for marketing campaigns, previews, and workflows."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Exists, OuterRef, Q, QuerySet

from quickstart.models import Booking, Contact

# Allowed on Starter (no advanced_segmentation).
STARTER_AUDIENCE_TYPES = frozenset(
    {"all_contacts", "tags", "contact_ids", "booking_channel"}
)


def audience_tier_error(tier: Optional[dict], audience_type: str, audience_filter: dict) -> Optional[str]:
    """Return user-facing error message if this audience is not allowed for the tier."""
    if not tier:
        return "Unknown email marketing plan."
    at = (audience_type or "all_contacts").strip()
    flt = audience_filter if isinstance(audience_filter, dict) else {}
    if at == "saved_segment":
        if not tier.get("saved_segments_enabled"):
            return "Saved audiences require Growth or higher email marketing."
        if not flt.get("segment_id"):
            return "segment_id is required for saved_segment audience."
        return None
    if not tier.get("advanced_segmentation") and at not in STARTER_AUDIENCE_TYPES:
        return "This audience target requires Growth or higher email marketing."
    return None


def _base_contacts(business) -> QuerySet:
    return (
        Contact.objects.filter(business=business)
        .exclude(email__isnull=True)
        .exclude(email="")
        .filter(marketing_unsubscribed=False)
    )


def _widget_source_q() -> Q:
    return (
        Q(source="widget_booking")
        | Q(source="widget")
        | Q(source__startswith="widget_")
        | Q(source__icontains="widget_booking")
    )


def build_contact_queryset(
    business,
    audience_type: Optional[str],
    audience_filter: Optional[Dict[str, Any]],
) -> Tuple[QuerySet, Dict[str, Any]]:
    """
    Returns (queryset, meta) where meta may include notes for debugging.
    audience_filter is a JSON dict; shapes depend on audience_type.
    """
    at = (audience_type or "all_contacts").strip()
    flt: Dict[str, Any] = audience_filter if isinstance(audience_filter, dict) else {}
    meta: Dict[str, Any] = {"audience_type": at}

    qs = _base_contacts(business)

    if at == "all_contacts":
        return qs, meta

    if at == "tags" and flt.get("tags"):
        tags = flt["tags"]
        if not isinstance(tags, list):
            tags = [tags]
        for t in tags:
            qs = qs.filter(tags__contains=[t])
        return qs.distinct(), meta

    if at == "contact_ids" and flt.get("contact_ids"):
        ids = flt["contact_ids"]
        if not isinstance(ids, list):
            ids = [ids]
        qs = qs.filter(id__in=ids)
        return qs, meta

    if at == "contact_source_in" and flt.get("sources"):
        sources = flt["sources"]
        if not isinstance(sources, list):
            sources = [sources]
        qs = qs.filter(source__in=[str(s) for s in sources])
        return qs, meta

    if at == "booking_channel":
        channel = (flt.get("channel") or "").strip().lower()
        if channel == "widget":
            qs = qs.filter(_widget_source_q())
        elif channel == "marketplace":
            qs = qs.filter(~_widget_source_q())
        return qs, meta

    if at == "booked_class":
        class_ids = flt.get("class_ids")
        if not class_ids:
            return qs.none(), meta
        if not isinstance(class_ids, list):
            class_ids = [class_ids]
        try:
            class_ids_int = [int(x) for x in class_ids]
        except (TypeError, ValueError):
            return qs.none(), meta
        bq = Booking.objects.filter(
            contact_id=OuterRef("pk"),
            schedule_instance__schedule__option__classId_id__in=class_ids_int,
        )
        status_in = flt.get("booking_status_in")
        if status_in and isinstance(status_in, list):
            bq = bq.filter(status__in=[str(s) for s in status_in])
        qs = qs.filter(Exists(bq))
        return qs, meta

    if at == "booked_any":
        bq = Booking.objects.filter(contact_id=OuterRef("pk"))
        status_in = flt.get("booking_status_in")
        if status_in and isinstance(status_in, list):
            bq = bq.filter(status__in=[str(s) for s in status_in])
        ba = flt.get("booked_after")
        bb = flt.get("booked_before")
        if ba:
            bq = bq.filter(booking_date__gte=ba)
        if bb:
            bq = bq.filter(booking_date__lte=bb)
        qs = qs.filter(Exists(bq))
        return qs, meta

    if at == "has_no_bookings":
        bq = Booking.objects.filter(contact_id=OuterRef("pk"))
        qs = qs.filter(~Exists(bq))
        return qs, meta

    if at == "saved_segment":
        from quickstart.models import MarketingSavedSegment

        sid = flt.get("segment_id")
        if not sid:
            return qs.none(), meta
        seg = MarketingSavedSegment.objects.filter(id=sid, business=business).first()
        if not seg:
            return qs.none(), meta
        return build_contact_queryset(business, seg.audience_type, seg.audience_filter or {})

    meta["warning"] = "unknown_audience_type"
    return qs, meta


def serialize_contact_sample(contact: Contact) -> Dict[str, Any]:
    return {
        "id": str(contact.id),
        "email": contact.email,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "source": contact.source,
    }
