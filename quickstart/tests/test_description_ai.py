"""Unit tests for description AI queue helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from quickstart.utils.description_ai import (
    description_ai_has_output,
    description_ai_needs_processing,
    description_ai_task_force,
)


def _cls(**kwargs):
    defaults = {
        "status": "active",
        "description": "A real class description.",
        "description_ai_status": "ready",
        "description_summary": "Short summary.",
        "description_sections": [{"id": "a", "title": "Hi", "body": "Body"}],
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestDescriptionAiHelpers:
    def test_has_output_summary_or_sections(self):
        assert description_ai_has_output(_cls(description_summary="x", description_sections=[]))
        assert description_ai_has_output(_cls(description_summary="", description_sections=[{"id": "1"}]))
        assert not description_ai_has_output(_cls(description_summary="", description_sections=[]))

    def test_needs_processing_on_description_change(self):
        assert description_ai_needs_processing(_cls(), description_changed=True)

    def test_needs_processing_when_pending_without_output(self):
        row = _cls(
            description_ai_status="pending",
            description_summary="",
            description_sections=[],
        )
        assert description_ai_needs_processing(row, description_changed=False)

    def test_skips_ready_with_output_when_unchanged(self):
        row = _cls(description_ai_status="ready")
        assert not description_ai_needs_processing(row, description_changed=False)

    def test_needs_processing_when_ready_but_output_missing(self):
        row = _cls(
            description_ai_status="ready",
            description_summary="",
            description_sections=[],
        )
        assert description_ai_needs_processing(row, description_changed=False)

    def test_task_force_for_pending_failed_stale(self):
        assert description_ai_task_force(_cls(description_ai_status="pending"))
        assert description_ai_task_force(_cls(description_ai_status="failed"))
        assert description_ai_task_force(_cls(description_ai_status="stale"))
        assert not description_ai_task_force(_cls(description_ai_status="ready"))


@pytest.mark.django_db
class TestDescriptionAiStuckQueryset:
    def test_includes_stale_and_pending_without_output(self):
        from quickstart.tests.factories import BusinessFactory, ClassMainFactory
        from quickstart.utils.description_ai import description_ai_stuck_queryset

        business = BusinessFactory(businessCity="Toronto")
        stale = ClassMainFactory(
            businessId=business,
            status="active",
            description="Has text",
            description_ai_status="stale",
            description_summary="",
            description_sections=[],
        )
        pending = ClassMainFactory(
            businessId=business,
            status="active",
            description="Also has text",
            description_ai_status="pending",
            description_summary="",
            description_sections=[],
        )
        ready = ClassMainFactory(
            businessId=business,
            status="active",
            description="Done",
            description_ai_status="ready",
            description_summary="Already formatted.",
            description_sections=[{"id": "a", "title": "Hi", "body": "x"}],
        )

        ids = set(description_ai_stuck_queryset().values_list("classId", flat=True))
        assert stale.classId in ids
        assert pending.classId in ids
        assert ready.classId not in ids
