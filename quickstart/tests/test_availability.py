"""Unit tests for appointment slot generation."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import pytz
from django.utils import timezone

from quickstart.models import (
    Booking,
    BusinessTimeOff,
    ClassOption,
    RecurrenceRule,
    Schedule,
    ScheduleInstance,
    ServiceAvailabilityWindow,
)
from quickstart.services.availability import generate_appointment_slots
from quickstart.services.recurrence import iter_rule_dates, materialize_rule
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    UserFactory,
)


def _appointment_service(business=None, **kwargs):
    business = business or BusinessFactory(
        businessHours=[
            {"day": "Mon", "isOpen": True, "open": "09:00", "close": "12:00"},
            {"day": "Tue", "isOpen": True, "open": "09:00", "close": "12:00"},
            {"day": "Wed", "isOpen": False, "open": "09:00", "close": "17:00"},
            {"day": "Thu", "isOpen": True, "open": "09:00", "close": "12:00"},
            {"day": "Fri", "isOpen": True, "open": "09:00", "close": "12:00"},
            {"day": "Sat", "isOpen": False, "open": "09:00", "close": "17:00"},
            {"day": "Sun", "isOpen": False, "open": "09:00", "close": "17:00"},
        ],
        business_timezone="America/Toronto",
    )
    defaults = dict(
        businessId=business,
        service_type="appointment",
        duration_minutes=60,
        price=Decimal("50.00"),
        capacity=1,
        slot_interval_minutes=30,
        max_concurrent=1,
        buffer_before_minutes=0,
        buffer_after_minutes=0,
        min_notice_hours=0,
        max_advance_days=30,
        status="active",
    )
    defaults.update(kwargs)
    service = ClassMainFactory(**defaults)
    ClassOptionFactory(classId=service, title="Standard")
    return service


@pytest.mark.django_db
def test_slots_respect_business_hours_and_interval():
    service = _appointment_service()
    # Pick a Monday at least a day out so min_notice doesn't wipe the window.
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    slots = generate_appointment_slots(service, monday, monday)
    times = [s["time"] for s in slots]
    assert "09:00:00" in times
    assert "09:30:00" in times
    assert "11:00:00" in times
    assert "11:30:00" not in times  # 60 min duration wouldn't finish by 12:00 from 11:30
    assert all(s["date"] == monday.isoformat() for s in slots)


@pytest.mark.django_db
def test_buffer_blocks_overlapping_slot():
    service = _appointment_service(buffer_after_minutes=15, slot_interval_minutes=15)
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    option = service.options.first()
    sched = ScheduleFactory(
        option=option,
        date=monday,
        time=time(9, 0),
        duration=60,
        price=Decimal("50.00"),
        maxParticipants=1,
    )
    inst = ScheduleInstanceFactory(
        schedule=sched,
        date=monday,
        time=time(9, 0),
        duration=60,
        price=Decimal("50.00"),
        max_participants=1,
    )
    BookingFactory(
        schedule_instance=inst,
        status="confirmed",
        payment_status="paid",
        participants=1,
        amount_paid=Decimal("50.00"),
    )
    slots = generate_appointment_slots(service, monday, monday)
    times = [s["time"] for s in slots]
    # 09:00 occupied; 09:15/09:30/10:00 overlap the 09:00-10:00 session plus 15m buffer.
    assert "09:00:00" not in times
    assert "10:00:00" not in times
    assert "10:15:00" in times


@pytest.mark.django_db
def test_max_concurrent_allows_parallel_bookings():
    service = _appointment_service(max_concurrent=2)
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    option = service.options.first()
    sched = ScheduleFactory(
        option=option,
        date=monday,
        time=time(9, 0),
        duration=60,
        price=Decimal("50.00"),
        maxParticipants=2,
    )
    inst = ScheduleInstanceFactory(
        schedule=sched,
        date=monday,
        time=time(9, 0),
        duration=60,
        price=Decimal("50.00"),
        max_participants=2,
    )
    BookingFactory(
        schedule_instance=inst,
        status="confirmed",
        payment_status="paid",
        participants=1,
        amount_paid=Decimal("50.00"),
    )
    slots = generate_appointment_slots(service, monday, monday)
    nine = next(s for s in slots if s["time"] == "09:00:00")
    assert nine["available"] == 1


@pytest.mark.django_db
def test_min_notice_hides_soon_slots():
    service = _appointment_service(min_notice_hours=48)
    tz = pytz.timezone("America/Toronto")
    today = timezone.now().astimezone(tz).date()
    slots = generate_appointment_slots(service, today, today)
    assert slots == []


@pytest.mark.django_db
def test_time_off_blocks_day():
    service = _appointment_service()
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    BusinessTimeOff.objects.create(
        business=service.businessId,
        title="Holiday",
        start_date=monday,
        end_date=monday,
        all_day=True,
    )
    slots = generate_appointment_slots(service, monday, monday)
    assert slots == []


@pytest.mark.django_db
def test_custom_service_window_overrides_business_hours():
    service = _appointment_service()
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    ServiceAvailabilityWindow.objects.create(
        service=service,
        weekday="Mon",
        start_time=time(14, 0),
        end_time=time(16, 0),
    )
    slots = generate_appointment_slots(service, monday, monday)
    times = [s["time"] for s in slots]
    assert "09:00:00" not in times
    assert "14:00:00" in times
    assert "15:00:00" in times


@pytest.mark.django_db
def test_recurrence_materialize_weekly_dates():
    service = _appointment_service(service_type="group")
    option = service.options.first()
    tz = pytz.timezone("America/Toronto")
    now = timezone.now().astimezone(tz).date()
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    rule = RecurrenceRule.objects.create(
        service=service,
        variant=option,
        weekdays=["Mon"],
        time=time(18, 0),
        start_date=monday,
        until_date=monday + timedelta(days=21),
        timezone="America/Toronto",
    )
    dates = list(iter_rule_dates(rule))
    assert all(d.weekday() == 0 for d in dates)
    assert len(dates) == 4
    created = materialize_rule(rule)
    assert len(created) == 4
    # Idempotent
    created_again = materialize_rule(rule)
    assert created_again == []
