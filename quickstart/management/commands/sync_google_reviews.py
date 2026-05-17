from django.core.management.base import BaseCommand

from quickstart.models import BusinessInfo
from quickstart.tasks.google_reviews_tasks import sync_google_reviews_for_business


class Command(BaseCommand):
    help = (
        "Queue Google Maps reviews sync (Apify) for one business or all with google_maps_url set."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--business-id",
            type=int,
            default=None,
            help="Only sync this BusinessInfo.businessId",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Sync all businesses that have google_maps_url set",
        )

    def handle(self, *args, **options):
        bid = options.get("business_id")
        do_all = options.get("all")

        if bid is not None:
            if not BusinessInfo.objects.filter(pk=bid).exists():
                self.stderr.write(f"No business with id={bid}")
                return
            sync_google_reviews_for_business.delay(bid)
            self.stdout.write(
                self.style.SUCCESS(f"Queued Google reviews sync for business {bid}")
            )
            return

        if do_all:
            pks = list(
                BusinessInfo.objects.exclude(google_maps_url__isnull=True)
                .exclude(google_maps_url="")
                .values_list("businessId", flat=True)
            )
            for pk in pks:
                sync_google_reviews_for_business.delay(pk)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Queued Google reviews sync for {len(pks)} businesses"
                )
            )
            return

        self.stderr.write("Specify --business-id N or --all")
