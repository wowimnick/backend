"""Appointment slot generation from business hours plus per-service controls."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytz
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from quickstart.models import (
    Booking,
    BusinessTimeOff,
    ScheduleInstance,
    ServiceAvailabilityWindow,
)

WEEKDAY_ABBREV = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_hhmm(value):
    if value is None:
        return None
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).time()
        except ValueError:
            continue
    return None


def _hours_for_weekday(business_hours, weekday_abbrev):
    """Return (open, close) or None if closed. Accepts both backend and form shapes."""
    if not business_hours:
        return None
    for row in business_hours:
        if not isinstance(row, dict):
            continue
        if row.get("day") != weekday_abbrev:
            continue
        if not row.get("isOpen"):
            return None
        open_t = _parse_hhmm(row.get("open"))
        close_t = _parse_hhmm(row.get("close"))
        pair = row.get("time")
        if (not open_t or not close_t) and isinstance(pair, (list, tuple)) and len(pair) >= 2:
            open_t = open_t or _parse_hhmm(pair[0])
            close_t = close_t or _parse_hhmm(pair[1])
        if open_t and close_t and open_t < close_t:
            return open_t, close_t
        return None
    return None


def _service_windows_for_weekday(service, weekday_abbrev):
    windows = list(
        ServiceAvailabilityWindow.objects.filter(service=service, weekday=weekday_abbrev)
    )
    if not windows:
        return None
    if any(w.is_closed for w in windows):
        return []
    return [(w.start_time, w.end_time) for w in windows if w.start_time < w.end_time]


def resolved_duration(service, variant=None):
    if variant and variant.duration_minutes:
        return int(variant.duration_minutes)
    return int(service.duration_minutes or 60)


def resolved_price(service, variant=None):
    if variant and variant.price is not None:
        return variant.price
    return service.price or Decimal("0.00")


def resolved_capacity(service, variant=None):
    if variant and variant.capacity:
        return int(variant.capacity)
    return int(service.capacity or 1)


def _combine_local(day, t, tz):
    naive = datetime.combine(day, t)
    return tz.localize(naive)


def _time_off_blocks(business, day):
    rows = BusinessTimeOff.objects.filter(
        business=business,
        start_date__lte=day,
        end_date__gte=day,
    )
    blocks = []
    for row in rows:
        if row.all_day or not row.start_time or not row.end_time:
            blocks.append((time(0, 0), time(23, 59, 59)))
        else:
            blocks.append((row.start_time, row.end_time))
    return blocks


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def _occupied_windows(service, day, tz):
    """Existing booked/scheduled windows on this day, including buffers."""
    buf_before = timedelta(minutes=int(service.buffer_before_minutes or 0))
    buf_after = timedelta(minutes=int(service.buffer_after_minutes or 0))
    instances = (
        ScheduleInstance.objects.filter(
            schedule__option__classId=service,
            date=day,
            status="scheduled",
        )
        .annotate(
            occupied=Coalesce(
                Sum(
                    "bookings__participants",
                    filter=Q(bookings__status__in=["confirmed", "pending"]),
                ),
                0,
            )
        )
    )
    windows = []
    for inst in instances:
        start = _combine_local(day, inst.time, tz)
        end = start + timedelta(minutes=int(inst.duration or 0))
        occupied = inst.occupied or 0
        if inst.status == "blackout":
            occupied = max(occupied, int(service.max_concurrent or 1))
        if occupied <= 0:
            continue
        windows.append(
            {
                "start": start - buf_before,
                "end": end + buf_after,
                "occupied": occupied,
            }
        )
    return windows


def generate_appointment_slots(service, start_date, end_date, variant=None):
    """
    Return a list of open appointment slots:
    [{date, time, datetime_iso, duration_minutes, price, available, max_concurrent}]
    """
    if getattr(service, "service_type", "group") != "appointment":
        return []

    business = service.businessId
    tz_name = business.business_timezone or "America/Toronto"
    tz = pytz.timezone(tz_name)
    now = timezone.now().astimezone(tz)

    duration = resolved_duration(service, variant)
    price = resolved_price(service, variant)
    interval = max(5, int(service.slot_interval_minutes or 30))
    max_concurrent = int(service.max_concurrent or 1)
    min_notice = timedelta(hours=int(service.min_notice_hours or 0))
    max_advance = int(service.max_advance_days or 60)
    buf_before = timedelta(minutes=int(service.buffer_before_minutes or 0))
    buf_after = timedelta(minutes=int(service.buffer_after_minutes or 0))

    latest_bookable = (now + timedelta(days=max_advance)).date()
    earliest = max(start_date, now.date())
    latest = min(end_date, latest_bookable)
    if earliest > latest:
        return []

    slots = []
    day = earliest
    while day <= latest:
        weekday = WEEKDAY_ABBREV[day.weekday()]
        custom = _service_windows_for_weekday(service, weekday)
        if custom is None:
            hours = _hours_for_weekday(business.businessHours, weekday)
            windows = [hours] if hours else []
        else:
            windows = custom

        time_off = _time_off_blocks(business, day)
        occupied = _occupied_windows(service, day, tz)

        for open_t, close_t in windows:
            cursor = datetime.combine(day, open_t)
            close_dt = datetime.combine(day, close_t)
            while cursor + timedelta(minutes=duration) <= close_dt:
                slot_start = tz.localize(cursor)
                slot_end = slot_start + timedelta(minutes=duration)
                slot_time = cursor.time().replace(second=0, microsecond=0)

                blocked = False
                for off_start, off_end in time_off:
                    if _overlaps(slot_time, (slot_end.astimezone(tz)).time(), off_start, off_end) or (
                        off_start == time(0, 0) and off_end >= time(23, 59)
                    ):
                        blocked = True
                        break
                if not blocked and off_covers_all_day(time_off):
                    blocked = True

                if not blocked and slot_start < now + min_notice:
                    blocked = True

                taken = 0
                if not blocked:
                    window_start = slot_start - buf_before
                    window_end = slot_end + buf_after
                    for occ in occupied:
                        if _overlaps(window_start, window_end, occ["start"], occ["end"]):
                            taken += occ["occupied"]
                    if taken >= max_concurrent:
                        blocked = True

                if not blocked:
                    slots.append(
                        {
                            "date": day.isoformat(),
                            "time": slot_time.strftime("%H:%M:%S"),
                            "datetime_iso": slot_start.isoformat(),
                            "duration_minutes": duration,
                            "price": str(price),
                            "available": max_concurrent - taken,
                            "max_concurrent": max_concurrent,
                            "instance_id": None,
                            "is_appointment": True,
                        }
                    )
                cursor += timedelta(minutes=interval)
        day += timedelta(days=1)
    return slots


def off_covers_all_day(time_off):
    return any(start == time(0, 0) and end >= time(23, 59) for start, end in time_off)


def materialize_appointment_instance(service, slot_date, slot_time, variant=None, staff=None):
    """Create (or reuse) a Schedule + ScheduleInstance for a booked appointment slot."""
    from quickstart.models import ClassOption, Schedule, ScheduleInstance

    if variant is None:
        variant = service.options.order_by("optionId").first()
        if variant is None:
            variant = ClassOption.objects.create(
                classId=service,
                title="Standard",
                booking_type="Single Session",
            )

    duration = resolved_duration(service, variant)
    price = resolved_price(service, variant)
    capacity = max(1, int(service.max_concurrent or 1))

    existing = ScheduleInstance.objects.filter(
        schedule__option=variant,
        date=slot_date,
        time=slot_time,
        status="scheduled",
    ).first()
    if existing:
        return existing

    day_abbr = slot_date.strftime("%a")
    schedule = Schedule.objects.create(
        option=variant,
        day=day_abbr,
        time=slot_time,
        duration=duration,
        price=price,
        maxParticipants=capacity,
        minParticipants=1,
        date=slot_date,
        name="Appointment",
    )
    return ScheduleInstance.objects.create(
        schedule=schedule,
        date=slot_date,
        time=slot_time,
        duration=duration,
        price=price,
        max_participants=capacity,
        min_participants=1,
        status="scheduled",
        assigned_staff=staff,
    )
