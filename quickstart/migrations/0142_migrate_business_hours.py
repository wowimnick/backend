from django.db import migrations
import datetime

# The default structure we want to create
DEFAULT_WEEK_HOURS = [
    {"day": "Mon", "isOpen": True, "open": "09:00", "close": "17:00"},
    {"day": "Tue", "isOpen": True, "open": "09:00", "close": "17:00"},
    {"day": "Wed", "isOpen": True, "open": "09:00", "close": "17:00"},
    {"day": "Thu", "isOpen": True, "open": "09:00", "close": "17:00"},
    {"day": "Fri", "isOpen": True, "open": "09:00", "close": "17:00"},
    {"day": "Sat", "isOpen": False, "open": "09:00", "close": "17:00"},
    {"day": "Sun", "isOpen": False, "open": "09:00", "close": "17:00"},
]


def migrate_existing_hours(apps, schema_editor):
    """
    Finds all businesses with the old openingTime/closingTime fields
    and populates the new businessHours JSON field.
    """
    BusinessInfo = apps.get_model("quickstart", "BusinessInfo")

    for business in BusinessInfo.objects.all():
        # Only process if businessHours is empty and old fields exist
        if not business.businessHours and business.openingTime and business.closingTime:
            new_hours = []

            # Convert old time objects to "HH:MM" strings
            open_str = business.openingTime.strftime("%H:%M")
            close_str = business.closingTime.strftime("%H:%M")

            for day_template in DEFAULT_WEEK_HOURS:
                day_info = day_template.copy()
                # Apply the old times to weekdays (Mon-Fri)
                if day_info["day"] in ["Mon", "Tue", "Wed", "Thu", "Fri"]:
                    day_info["isOpen"] = True
                    day_info["open"] = open_str
                    day_info["close"] = close_str
                else:  # Weekends are closed by default
                    day_info["isOpen"] = False
                new_hours.append(day_info)

            business.businessHours = new_hours
            business.save(update_fields=["businessHours"])


def revert_hours_migration(apps, schema_editor):
    """
    Reverts the migration by trying to populate the old time fields
    from Monday's hours in the new JSON field.
    """
    BusinessInfo = apps.get_model("quickstart", "BusinessInfo")
    for business in BusinessInfo.objects.filter(businessHours__isnull=False):
        if (
            business.businessHours
            and isinstance(business.businessHours, list)
            and len(business.businessHours) > 0
        ):
            monday_hours = next(
                (item for item in business.businessHours if item.get("day") == "Mon"),
                None,
            )
            if monday_hours and monday_hours.get("isOpen"):
                try:
                    business.openingTime = datetime.datetime.strptime(
                        monday_hours["open"], "%H:%M"
                    ).time()
                    business.closingTime = datetime.datetime.strptime(
                        monday_hours["close"], "%H:%M"
                    ).time()
                    business.save(update_fields=["openingTime", "closingTime"])
                except (ValueError, TypeError):
                    # Could not parse, skip this record
                    pass


class Migration(migrations.Migration):

    dependencies = [
        (
            "quickstart",
            "0141_businessinfo_businesshours_businessinfo_businessunit_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(
            migrate_existing_hours, reverse_code=revert_hours_migration
        ),
    ]
