"""Materialize and edit RecurrenceRule series (this session vs this and following)."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from quickstart.models import RecurrenceRule, Schedule, ScheduleInstance
from quickstart.services.availability import resolved_capacity, resolved_duration, resolved_price

WEEKDAY_TO_NUM = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}


def iter_rule_dates(rule, from_date=None, until_date=None):
    start = from_date or rule.start_date
    end = until_date or rule.until_date
    if end is None:
        end = start + timedelta(weeks=12)
    weekdays = {WEEKDAY_TO_NUM.get(d, -1) for d in (rule.weekdays or [])}
    weekdays.discard(-1)
    if not weekdays:
        return
    current = start
    while current <= end:
        if current.weekday() in weekdays:
            yield current
        current += timedelta(days=1)


def materialize_rule(rule, from_date=None, until_date=None):
    """Create Schedule + ScheduleInstance rows for dates that do not already exist."""
    service = rule.service
    variant = rule.variant or service.options.order_by("optionId").first()
    if variant is None:
        return []

    duration = rule.duration_minutes or resolved_duration(service, variant)
    price = rule.price if rule.price is not None else resolved_price(service, variant)
    capacity = rule.capacity or resolved_capacity(service, variant)
    created = []

    existing = set(
        ScheduleInstance.objects.filter(recurrence_rule=rule).values_list("date", "time")
    )
    # Also skip any instance on this variant at the same date+time
    existing |= set(
        ScheduleInstance.objects.filter(
            schedule__option=variant, time=rule.time
        ).values_list("date", "time")
    )

    with transaction.atomic():
        for day in iter_rule_dates(rule, from_date=from_date, until_date=until_date):
            key = (day, rule.time)
            if key in existing:
                continue
            day_abbr = day.strftime("%a")
            schedule = Schedule.objects.create(
                option=variant,
                name="Series",
                day=day_abbr,
                time=rule.time,
                duration=duration,
                price=price,
                maxParticipants=capacity,
                minParticipants=1,
                date=day,
            )
            inst = ScheduleInstance.objects.create(
                schedule=schedule,
                recurrence_rule=rule,
                assigned_staff=rule.assigned_staff,
                date=day,
                time=rule.time,
                duration=duration,
                price=price,
                max_participants=capacity,
                min_participants=1,
                status="scheduled",
            )
            created.append(inst)
            existing.add(key)
    return created


def edit_this_session_only(instance, **changes):
    """Detach an instance from its series and apply local overrides."""
    instance.recurrence_rule = None
    for field, value in changes.items():
        if hasattr(instance, field) and value is not None:
            setattr(instance, field, value)
    instance.save()
    schedule = instance.schedule
    mapping = {
        "time": "time",
        "duration": "duration",
        "price": "price",
        "max_participants": "maxParticipants",
        "date": "date",
    }
    for inst_field, sched_field in mapping.items():
        if inst_field in changes and changes[inst_field] is not None:
            setattr(schedule, sched_field, changes[inst_field])
    if "date" in changes and changes["date"]:
        schedule.day = changes["date"].strftime("%a")
    schedule.save()
    return instance


def edit_this_and_following(instance, **changes):
    """Split the series at this instance: close the old rule, start a new one."""
    rule = instance.recurrence_rule
    if not rule:
        return edit_this_session_only(instance, **changes)

    split_date = instance.date
    old_until = rule.until_date
    rule.until_date = split_date - timedelta(days=1)
    if rule.until_date < rule.start_date:
        rule.is_active = False
    rule.save(update_fields=["until_date", "is_active", "updated_at"])

    new_time = changes.get("time", rule.time)
    new_duration = changes.get("duration", rule.duration_minutes)
    new_price = changes.get("price", rule.price)
    new_capacity = changes.get("max_participants", rule.capacity)
    new_staff = changes.get("assigned_staff", rule.assigned_staff)

    new_rule = RecurrenceRule.objects.create(
        service=rule.service,
        variant=rule.variant,
        weekdays=list(rule.weekdays or []),
        time=new_time,
        duration_minutes=new_duration,
        price=new_price,
        capacity=new_capacity,
        start_date=split_date,
        until_date=old_until,
        timezone=rule.timezone,
        assigned_staff=new_staff,
        is_active=True,
    )

    following = ScheduleInstance.objects.filter(
        recurrence_rule=rule, date__gte=split_date, status="scheduled"
    ).select_related("schedule")
    for inst in following:
        inst.recurrence_rule = new_rule
        if "time" in changes:
            inst.time = new_time
            inst.schedule.time = new_time
        if "duration" in changes and changes["duration"] is not None:
            inst.duration = changes["duration"]
            inst.schedule.duration = changes["duration"]
        if "price" in changes and changes["price"] is not None:
            inst.price = changes["price"]
            inst.schedule.price = changes["price"]
        if "max_participants" in changes and changes["max_participants"] is not None:
            inst.max_participants = changes["max_participants"]
            inst.schedule.maxParticipants = changes["max_participants"]
        if new_staff is not None or "assigned_staff" in changes:
            inst.assigned_staff = new_staff
        inst.save()
        inst.schedule.save()
    return new_rule


def delete_this_and_following(instance, reason="Series cancelled"):
    rule = instance.recurrence_rule
    if not rule:
        instance.status = "cancelled"
        instance.cancellation_reason = reason
        instance.save(update_fields=["status", "cancellation_reason"])
        return 1
    following = ScheduleInstance.objects.filter(
        recurrence_rule=rule, date__gte=instance.date, status="scheduled"
    )
    count = following.update(status="cancelled", cancellation_reason=reason)
    rule.until_date = instance.date - timedelta(days=1)
    if rule.until_date < rule.start_date:
        rule.is_active = False
    rule.save(update_fields=["until_date", "is_active", "updated_at"])
    return count
