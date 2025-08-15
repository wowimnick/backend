# FILE: quickstart/management/commands/run_test_setup.py

import sys
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from datetime import timedelta
import pytz

from quickstart.models import Booking, CustomUser, ScheduleInstance, BusinessInfo


def setup_reminder_test_data():
    """
    Sets up data for the booking reminder task with timezone awareness.
    It calculates the correct local time for a business that will fall
    within the Celery task's UTC-based reminder window.
    """
    print("--- Starting Timezone-Aware Test Data Setup for Booking Reminder ---")

    # --- 1. Define the TARGET time in UTC ---
    # We want to create a booking that will occur in exactly 23.5 hours from now.
    now = timezone.now()
    target_utc_datetime = now + timedelta(hours=23, minutes=30)

    # --- 2. Find a business and its timezone ---
    # The booking needs to belong to a specific business to get its timezone.
    instance_template = (
        ScheduleInstance.objects.select_related("schedule__option__classId__businessId")
        .order_by("?")
        .first()
    )

    if not instance_template:
        raise CommandError(
            "ERROR: No ScheduleInstances found. Create a class/schedule first."
        )

    business = instance_template.schedule.option.classId.businessId
    try:
        business_tz = pytz.timezone(business.business_timezone)
    except pytz.UnknownTimeZoneError:
        raise CommandError(
            f"ERROR: Business {business.businessId} has an invalid timezone: '{business.business_timezone}'"
        )

    print(
        f"Using template from Business ID {business.businessId} in timezone '{business.business_timezone}'"
    )

    # --- 3. Convert the target UTC time to the business's LOCAL time ---
    # This is the key step. We find out what "time on the clock" it will be
    # in the business's location at our target UTC moment.
    target_local_datetime = target_utc_datetime.astimezone(business_tz)
    target_local_date = target_local_datetime.date()
    target_local_time = target_local_datetime.time()

    print(
        f"Target UTC time:      {target_utc_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    print(
        f"Equivalent Local Time:  {target_local_datetime.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    # --- 4. Find a user ---
    user_to_book = CustomUser.objects.order_by("?").first()
    if not user_to_book:
        raise CommandError("ERROR: No users found. Please create a user first.")
    print(f"Found user to book: {user_to_book.email}")

    # --- 5. Create the ScheduleInstance using the LOCAL date and time ---
    test_instance, _ = ScheduleInstance.objects.update_or_create(
        schedule=instance_template.schedule,
        date=target_local_date,
        defaults={
            "time": target_local_time,
            "duration": instance_template.duration,
            "price": instance_template.price,
            "max_participants": instance_template.max_participants,
            "status": "scheduled",
        },
    )
    print(
        f"Ensured test ScheduleInstance exists with ID: {test_instance.id} for the calculated local time."
    )

    # --- 6. Create the Booking ---
    test_booking, _ = Booking.objects.update_or_create(
        user=user_to_book,
        schedule_instance=test_instance,
        defaults={
            "status": "confirmed",
            "payment_status": "paid",
            "participants": 1,
            "amount_paid": test_instance.price,
        },
    )
    print(f"SUCCESS: Ensured test Booking exists with ID: {test_booking.id}")
    print("\n--- Reminder Test Data is Ready. You can now trigger the task. ---")


class Command(BaseCommand):
    help = "Sets up test data required for triggering Celery tasks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--task",
            type=str,
            help='Specify which task data to set up: "reminder"',
            required=True,
        )

    def handle(self, *args, **options):
        task_type = options["task"]

        if task_type == "reminder":
            setup_reminder_test_data()
        else:
            self.stdout.write(
                self.style.ERROR(f'Invalid task type "{task_type}". Use "reminder".')
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f'Successfully set up data for the "{task_type}" task.')
        )
