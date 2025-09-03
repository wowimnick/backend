# quickstart/management/commands/backfill_contacts.py

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from quickstart.models import BusinessInfo, CustomUser, Contact, Booking


class Command(BaseCommand):
    help = "Backfills missing Contact records for users who have existing bookings with a business."

    def handle(self, *args, **options):
        self.stdout.write("Starting to backfill missing Contact records...")
        total_contacts_created = 0

        # Get all businesses that have at least one booking
        businesses = BusinessInfo.objects.filter(
            classes__options__schedules__instances__bookings__isnull=False
        ).distinct()

        for business in businesses:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nProcessing business: {business.businessName} (ID: {business.businessId})"
                )
            )

            # 1. Find all users who have ever booked with this business
            users_with_bookings = CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business
            ).distinct()

            # 2. Find all users who ALREADY have a contact record for this business
            # --- THIS IS THE CORRECTED LINE ---
            users_with_contacts = CustomUser.objects.filter(
                contact_profiles__business=business
            )
            # --- END OF CORRECTION ---

            # 3. Determine which users are missing a contact record
            users_to_create_contact_for = users_with_bookings.exclude(
                pk__in=users_with_contacts.values_list("pk", flat=True)
            )

            if not users_to_create_contact_for.exists():
                self.stdout.write(
                    "All students already have contact records. Nothing to do."
                )
                continue

            self.stdout.write(
                f"Found {users_to_create_contact_for.count()} students missing a Contact record."
            )

            contacts_to_create = []
            for user in users_to_create_contact_for:
                contacts_to_create.append(
                    Contact(
                        business=business,
                        user=user,
                        first_name=user.first_name or "",
                        last_name=user.last_name or "",
                        email=user.email,
                        phone_number=user.phone_number or "",
                        source="platform_booking_backfill",
                    )
                )

            if contacts_to_create:
                try:
                    with transaction.atomic():
                        Contact.objects.bulk_create(contacts_to_create)
                        total_contacts_created += len(contacts_to_create)
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"Successfully created {len(contacts_to_create)} new Contact records."
                            )
                        )
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(
                            f"An error occurred while creating contacts for {business.businessName}: {e}"
                        )
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nOperation complete. Total new contacts created: {total_contacts_created}"
            )
        )
