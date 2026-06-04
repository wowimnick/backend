"""Helpers for exposing Celery Beat scheduled tasks to the admin metrics API."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _humanize_celery_schedule(schedule) -> str:
    """Readable description for crontab / interval schedules."""
    run_every = getattr(schedule, "run_every", None)
    if run_every is not None:
        total = int(run_every.total_seconds())
        if total % 86400 == 0 and total >= 86400:
            days = total // 86400
            return f"Every {days} day{'s' if days != 1 else ''}"
        if total % 3600 == 0 and total >= 3600:
            hours = total // 3600
            return f"Every {hours} hour{'s' if hours != 1 else ''}"
        if total % 60 == 0 and total >= 60:
            minutes = total // 60
            return f"Every {minutes} minute{'s' if minutes != 1 else ''}"
        return f"Every {total} seconds"

    minute = getattr(schedule, "_orig_minute", getattr(schedule, "minute", "*"))
    hour = getattr(schedule, "_orig_hour", getattr(schedule, "hour", "*"))
    day_of_week = getattr(
        schedule, "_orig_day_of_week", getattr(schedule, "day_of_week", "*")
    )
    day_of_month = getattr(
        schedule, "_orig_day_of_month", getattr(schedule, "day_of_month", "*")
    )
    month_of_year = getattr(
        schedule, "_orig_month_of_year", getattr(schedule, "month_of_year", "*")
    )

    def _fmt(value):
        if value in (None, "*", set("*")):
            return "*"
        if isinstance(value, (set, list, tuple)):
            return ",".join(str(v) for v in sorted(value))
        return str(value)

    minute_s, hour_s = _fmt(minute), _fmt(hour)
    dow_s, dom_s, month_s = _fmt(day_of_week), _fmt(day_of_month), _fmt(month_of_year)

    if minute_s.startswith("*/"):
        step = minute_s[2:]
        return f"Every {step} minutes"
    if hour_s.startswith("*/"):
        step = hour_s[2:]
        return f"Every {step} hours"
    if minute_s == "*" and hour_s == "*" and dow_s == dom_s == month_s == "*":
        return "Every minute"
    if minute_s != "*" and hour_s == "*" and dow_s == dom_s == month_s == "*":
        return f"At minute {minute_s} of every hour"
    if minute_s != "*" and hour_s != "*" and dow_s == dom_s == month_s == "*":
        return f"Daily at {hour_s.zfill(2)}:{minute_s.zfill(2)} UTC"
    if dom_s != "*":
        return f"Cron {minute_s} {hour_s} {dom_s} {month_s} {dow_s} (UTC)"
    if dow_s != "*":
        return f"Cron {minute_s} {hour_s} * * {dow_s} (UTC)"
    return f"Cron {minute_s} {hour_s} {dom_s} {month_s} {dow_s} (UTC)"


def _task_entry(
    *,
    key,
    label,
    task_path,
    schedule,
    source,
    enabled=True,
    last_run_at=None,
):
    now = timezone.now()
    remaining = schedule.remaining_estimate(now)
    if remaining < timedelta(0):
        remaining = timedelta(0)
    next_run = now + remaining
    local_next = timezone.localtime(next_run)
    tz = timezone.get_current_timezone()

    return {
        "key": key,
        "label": label,
        "task": task_path,
        "schedule_description": _humanize_celery_schedule(schedule),
        "next_run_at": next_run.isoformat(),
        "next_run_local": local_next.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds_until_next": max(0, int(remaining.total_seconds())),
        "enabled": enabled,
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "source": source,
        "schedule_timezone": str(tz),
    }


def get_beat_schedule_tasks():
    """
    Return upcoming Celery Beat tasks with next run time and countdown.

    Prefers django-celery-beat PeriodicTask rows (production DatabaseScheduler),
    then supplements with static CELERY_BEAT_SCHEDULE entries not present in DB.
    """
    tasks = []
    seen_keys = set()

    try:
        from django_celery_beat.models import PeriodicTask

        periodic_qs = (
            PeriodicTask.objects.filter(enabled=True)
            .select_related("crontab", "interval", "solar", "clocked")
            .order_by("name")
        )
        for pt in periodic_qs:
            try:
                schedule = pt.schedule
            except Exception as exc:
                logger.warning("Skipping periodic task %s: %s", pt.name, exc)
                continue
            if schedule is None:
                continue
            seen_keys.add(pt.name)
            tasks.append(
                _task_entry(
                    key=pt.name,
                    label=pt.name.replace("-", " ").replace("_", " ").title(),
                    task_path=pt.task,
                    schedule=schedule,
                    source="database",
                    enabled=pt.enabled,
                    last_run_at=pt.last_run_at,
                )
            )
    except Exception as exc:
        logger.warning("Could not load django-celery-beat PeriodicTask rows: %s", exc)

    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}
    for key, entry in beat.items():
        if key in seen_keys:
            continue
        if not isinstance(entry, dict):
            continue
        schedule = entry.get("schedule")
        task_path = entry.get("task", "")
        if not schedule or not task_path:
            continue
        try:
            tasks.append(
                _task_entry(
                    key=key,
                    label=key.replace("-", " ").replace("_", " ").title(),
                    task_path=task_path,
                    schedule=schedule,
                    source="settings",
                    enabled=True,
                    last_run_at=None,
                )
            )
        except Exception as exc:
            logger.warning("Skipping settings beat entry %s: %s", key, exc)

    tasks.sort(key=lambda item: item["seconds_until_next"])
    return tasks
