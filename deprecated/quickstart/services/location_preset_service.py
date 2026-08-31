"""Location presets filtered by active class coverage inside census boundaries."""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from quickstart.constants.explore_location_presets import EXPLORE_LOCATION_PRESET_CATALOG
from quickstart.models import ClassesMain, GeographicBoundary, ScheduleInstance

# Keep homepage / header location shortcuts short even as inventory grows.
MAX_UI_LOCATION_PRESETS = 8


def _find_boundary_for_preset(name: str) -> GeographicBoundary | None:
    if not name:
        return None
    return (
        GeographicBoundary.objects.filter(
            Q(name__iexact=name) | Q(name__istartswith=f"{name} (")
        )
        .only("id", "name", "geom")
        .first()
    )


def _active_bookable_classes_queryset():
    has_future_instances = ScheduleInstance.objects.filter(
        schedule__option__classId=OuterRef("pk"),
        date__gte=timezone.now().date(),
        status="scheduled",
    )
    return ClassesMain.objects.filter(
        status="active",
        businessId__isActive=True,
        businessId__verificationStatus="verified",
        point__isnull=False,
    ).filter(Exists(has_future_instances))


def get_location_presets_with_coverage(
    *, min_count: int = 1, max_presets: int = MAX_UI_LOCATION_PRESETS
) -> list[dict[str, Any]]:
    """
    Return catalog presets that have at least min_count bookable classes whose
    point lies inside the matching GeographicBoundary polygon (no radius fallback).
    """
    if min_count < 1:
        min_count = 1
    max_presets = max(1, int(max_presets or MAX_UI_LOCATION_PRESETS))

    base_qs = _active_bookable_classes_queryset()
    covered: list[dict[str, Any]] = []

    for preset in EXPLORE_LOCATION_PRESET_CATALOG:
        boundary = _find_boundary_for_preset(preset["name"])
        if boundary is None:
            continue
        class_count = base_qs.filter(point__within=boundary.geom).count()
        if class_count < min_count:
            continue
        covered.append(
            {
                **preset,
                "classCount": class_count,
            }
        )

    # Toronto stays first; remaining presets sort by inventory (desc).
    if len(covered) <= 1:
        return covered

    toronto = next((p for p in covered if p["name"] == "Toronto"), None)
    rest = [p for p in covered if p["name"] != "Toronto"]
    rest.sort(key=lambda row: (-int(row.get("classCount") or 0), row["name"]))
    if toronto:
        rest = rest[: max(0, max_presets - 1)]
        return [toronto, *rest]
    return rest[:max_presets]


def serialize_presets_for_api(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip internal fields not needed by the frontend dropdown."""
    return [
        {
            "name": p["name"],
            "displayName": p["displayName"],
            "description": p["description"],
            "coords": p["coords"],
            "provinceSlug": p["provinceSlug"],
            "citySlug": p["citySlug"],
            "classCount": int(p.get("classCount") or 0),
        }
        for p in presets
    ]
