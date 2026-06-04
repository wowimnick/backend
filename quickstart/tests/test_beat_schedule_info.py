"""Tests for Celery Beat schedule info exposed to admin metrics."""

import pytest
from django.utils import timezone

from quickstart.utils.beat_schedule_info import (
    get_beat_schedule_tasks,
    _humanize_celery_schedule,
)


@pytest.mark.django_db
def test_get_beat_schedule_tasks_from_settings():
    tasks = get_beat_schedule_tasks()
    assert isinstance(tasks, list)
    assert len(tasks) >= 1
    first = tasks[0]
    assert "key" in first
    assert "task" in first
    assert "schedule_description" in first
    assert "next_run_at" in first
    assert "seconds_until_next" in first
    assert first["seconds_until_next"] >= 0


def test_humanize_every_five_minutes_crontab(settings):
    from celery.schedules import crontab

    desc = _humanize_celery_schedule(crontab(minute="*/5"))
    assert "5" in desc
    assert "minute" in desc.lower()


def test_task_entry_next_run_is_in_future(settings):
    from celery.schedules import crontab
    from django.utils.dateparse import parse_datetime

    from quickstart.utils.beat_schedule_info import _task_entry

    now = timezone.now()
    entry = _task_entry(
        key="test-task",
        label="Test Task",
        task_path="quickstart.tasks.booking_tasks.release_expired_spots",
        schedule=crontab(minute="*/5"),
        source="settings",
    )
    next_run = parse_datetime(entry["next_run_at"])
    assert next_run is not None
    assert next_run >= now
