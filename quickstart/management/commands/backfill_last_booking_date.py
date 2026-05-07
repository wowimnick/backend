"""Backfill BusinessInfo.last_booking_date from Booking.booking_date (idempotent)."""

from django.core.management.base import BaseCommand
from django.db.models import Max

from quickstart.models import Booking, BusinessInfo


class Command(BaseCommand):
    help = "Set BusinessInfo.last_booking_date to max(booking_date) per business from the booking chain."

    def handle(self, *args, **options):
        rows = (
            Booking.objects.filter(schedule_instance__isnull=False)
            .values(
                "schedule_instance__schedule__option__classId__businessId",
            )
            .annotate(mx=Max("booking_date"))
        )
        n = 0
        for r in rows:
            bid = r["schedule_instance__schedule__option__classId__businessId"]
            mx = r["mx"]
            if not bid or not mx:
                continue
            BusinessInfo.objects.filter(pk=bid).update(last_booking_date=mx)
            n += 1
        self.stdout.write(
            self.style.SUCCESS(f"backfill_last_booking_date: updated {n} businesses")
        )
