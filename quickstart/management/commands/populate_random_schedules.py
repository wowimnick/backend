"""Add future schedules with random dates/times for eligible classes (local/dev seeding)."""

from __future__ import annotations

import random
from datetime import time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from quickstart.models import ClassOption, Schedule, ScheduleInstance

_DAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _weekday_key(d):
    return _DAY_KEYS[d.weekday()]


def _random_session_time(rng: random.Random):
    h = rng.randint(8, 20)
    m = rng.choice([0, 15, 30, 45])
    return time(h, m)


def _template_for_option(option: ClassOption) -> dict:
    existing = (
        Schedule.objects.filter(option=option)
        .order_by("pk")
        .values(
            "price",
            "duration",
            "maxParticipants",
            "minParticipants",
        )
        .first()
    )
    if existing:
        return {
            "price": existing["price"],
            "duration": existing["duration"],
            "maxParticipants": existing["maxParticipants"],
            "minParticipants": existing["minParticipants"],
        }
    return {
        "price": Decimal("25.00"),
        "duration": 60,
        "maxParticipants": 10,
        "minParticipants": 1,
    }


class Command(BaseCommand):
    help = (
        "Create future schedules (and instances) for active classes on active businesses. "
        "Skips inactive/suspended classes, inactive businesses, and synced ticket tiers."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=1,
            help="How many schedules to add per option (each full-course option gets this many weekly series).",
        )
        parser.add_argument(
            "--max-days-ahead",
            type=int,
            default=90,
            help="Random session/course start dates fall between tomorrow and this many days ahead.",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Optional RNG seed for reproducible runs.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Add schedules even when the option already has future scheduled instances.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be created without writing to the database.",
        )

    def handle(self, *args, **options):
        count = max(1, options["count"])
        max_days = max(2, options["max_days_ahead"])
        seed = options["seed"]
        force = options["force"]
        dry_run = options["dry_run"]

        rng = random.Random(seed)
        today = timezone.now().date()

        options_qs = (
            ClassOption.objects.filter(
                classId__status="active",
                classId__businessId__isActive=True,
            )
            .exclude(schedule_mode="synced")
            .select_related("classId", "classId__businessId")
            .order_by("optionId")
        )

        created_schedules = 0
        created_instances = 0
        skipped = 0

        for option in options_qs:
            has_future = ScheduleInstance.objects.filter(
                schedule__option=option,
                date__gte=today,
                status="scheduled",
            ).exists()

            if has_future and not force:
                skipped += 1
                continue

            tpl = _template_for_option(option)
            n_here = count

            for _ in range(n_here):
                if option.booking_type == "Full Course":
                    day_key = rng.choice(_DAY_KEYS)
                    target_weekday = _DAY_KEYS.index(day_key)
                    start_offset = rng.randint(7, max_days)
                    start_date = today + timedelta(days=start_offset)
                    while start_date.weekday() != target_weekday:
                        start_date += timedelta(days=1)
                    span_weeks = rng.randint(4, 12)
                    end_date = start_date + timedelta(weeks=span_weeks)

                    if dry_run:
                        self.stdout.write(
                            f"[dry-run] Full course option {option.optionId} "
                            f"class={option.classId.slug} day={day_key} "
                            f"{start_date} -> {end_date}"
                        )
                        created_schedules += 1
                        continue

                    with transaction.atomic():
                        sched = Schedule(
                            option=option,
                            day=day_key,
                            time=_random_session_time(rng),
                            start_date=start_date,
                            end_date=end_date,
                            date=None,
                            price=tpl["price"],
                            duration=tpl["duration"],
                            maxParticipants=tpl["maxParticipants"],
                            minParticipants=tpl["minParticipants"],
                            name="",
                        )
                        sched.save()
                        inst_list = sched.generate_course_instances()
                        created_schedules += 1
                        created_instances += len(inst_list)
                    continue

                # Single session
                offset = rng.randint(1, max_days)
                session_date = today + timedelta(days=offset)
                t = _random_session_time(rng)

                if dry_run:
                    self.stdout.write(
                        f"[dry-run] Single session option {option.optionId} "
                        f"class={option.classId.slug} {session_date} {t}"
                    )
                    created_schedules += 1
                    continue

                with transaction.atomic():
                    sched = Schedule(
                        option=option,
                        day=_weekday_key(session_date),
                        date=session_date,
                        time=t,
                        price=tpl["price"],
                        duration=tpl["duration"],
                        maxParticipants=tpl["maxParticipants"],
                        minParticipants=tpl["minParticipants"],
                        name="",
                    )
                    sched.save()
                    ScheduleInstance.objects.create(
                        schedule=sched,
                        date=sched.date,
                        time=sched.time,
                        duration=sched.duration,
                        price=sched.price,
                        max_participants=sched.maxParticipants,
                        min_participants=sched.minParticipants,
                        status="scheduled",
                    )
                    created_schedules += 1
                    created_instances += 1

        msg = (
            f"populate_random_schedules: schedules={created_schedules}, "
            f"instances={created_instances}, skipped_options={skipped} "
            f"(already had future instances; use --force to add anyway)"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f"{msg} (dry-run)"))
        else:
            self.stdout.write(self.style.SUCCESS(msg))
