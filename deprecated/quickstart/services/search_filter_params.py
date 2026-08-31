"""Normalize public search query params shared by Postgres and Typesense paths."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BOOKING_TYPE_SINGLE_SESSION = "Single Session"
BOOKING_TYPE_FULL_COURSE = "Full Course"


def normalize_booking_type_query(raw: str | None) -> str | None:
    """
    Map ?class_type=... to ClassOption.booking_type values.
    Returns None when no filter (all types).
    """
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return None
    lower = key.lower().replace("-", "_")

    if lower in ("class", "all", "any"):
        return None

    if lower in (
        "single_session",
        "single session",
        "singlesession",
        "single",
        "drop_in",
        "dropin",
        "session",
        "workshop",
    ):
        return BOOKING_TYPE_SINGLE_SESSION

    if lower in (
        "full_course",
        "full course",
        "fullcourse",
        "course",
        "multi_session",
        "multisession",
        "series",
    ):
        return BOOKING_TYPE_FULL_COURSE

    if key == BOOKING_TYPE_SINGLE_SESSION:
        return BOOKING_TYPE_SINGLE_SESSION
    if key == BOOKING_TYPE_FULL_COURSE:
        return BOOKING_TYPE_FULL_COURSE

    logger.warning("Ignoring unknown class_type query value: %r", raw)
    return None
