"""Link orphan bookings to contacts and recompute CRM stats."""
from django.core.management.base import BaseCommand

from quickstart.models import Booking, Contact
from quickstart.services.crm_stats import backfill_contacts_and_stats


class Command(BaseCommand):
    help = (
        "For bookings with a user/email but no contact, get_or_create a Contact "
        "on the booking's business, link booking.contact, then refresh CRM stats "
        "for all contacts. Also runs automatically from migration 0252."
    )

    def handle(self, *args, **options):
        result = backfill_contacts_and_stats(Booking, Contact)
        self.stdout.write(
            self.style.SUCCESS(
                f"backfill_contact_stats: linked {result['linked']} bookings "
                f"({result['created']} contacts created, {result['skipped']} skipped); "
                f"refreshed {result['refreshed']} contacts"
            )
        )
