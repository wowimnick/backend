"""Helpers for Gemini description formatting (summary + sections)."""

from __future__ import annotations

from quickstart.models import ClassesMain


def description_ai_has_output(instance: ClassesMain) -> bool:
    summary = (getattr(instance, "description_summary", None) or "").strip()
    sections = getattr(instance, "description_sections", None) or []
    return bool(summary or (isinstance(sections, list) and len(sections) > 0))


def description_ai_needs_processing(
    instance: ClassesMain,
    *,
    description_changed: bool = False,
    became_active: bool = False,
) -> bool:
    """
    Return True when we should queue format_class_description_task.

    Besides description edits, re-queue when output is missing or status is not ready
    (recovers rows stuck on ``pending`` after a failed/lost worker run).
    """
    raw = (getattr(instance, "description", None) or "").strip()
    if not raw:
        return False
    if getattr(instance, "status", None) != "active":
        return False
    if description_changed or became_active:
        return True

    ai_status = getattr(instance, "description_ai_status", None) or "stale"
    if ai_status in ("stale", "failed", "pending"):
        return True
    if ai_status == "ready" and not description_ai_has_output(instance):
        return True
    return False


def description_ai_task_force(instance: ClassesMain) -> bool:
    """Force Gemini when retrying failed/pending/stale rows."""
    ai_status = getattr(instance, "description_ai_status", None) or "stale"
    if ai_status in ("failed", "pending", "stale"):
        return True
    if ai_status == "ready" and not description_ai_has_output(instance):
        return True
    return False


def description_ai_stuck_queryset():
    """
    Active classes that still need Gemini output: ``stale`` (never run) or
    ``pending`` (queued but no summary/sections yet).
    """
    from django.db.models import Q

    from quickstart.models import ClassesMain

    return (
        ClassesMain.objects.filter(
            status="active",
            description_ai_status__in=("pending", "stale"),
        )
        .exclude(description__exact="")
        .filter(
            Q(description_summary="") | Q(description_summary__isnull=True),
            description_sections=[],
        )
    )
