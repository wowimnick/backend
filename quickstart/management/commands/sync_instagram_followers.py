from django.core.management.base import BaseCommand

from quickstart.models import BusinessInfo
from quickstart.tasks.instagram_tasks import sync_instagram_followers_for_business


class Command(BaseCommand):
    help = "Queue Instagram follower sync (Apify) for one business or all with an Instagram URL."

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
            help="Sync all businesses that have social_media_links.instagram set",
        )

    def handle(self, *args, **options):
        bid = options.get("business_id")
        do_all = options.get("all")

        if bid is not None:
            if not BusinessInfo.objects.filter(pk=bid).exists():
                self.stderr.write(f"No business with id={bid}")
                return
            sync_instagram_followers_for_business.delay(bid)
            self.stdout.write(self.style.SUCCESS(f"Queued Instagram sync for business {bid}"))
            return

        if do_all:
            pks = list(
                BusinessInfo.objects.filter(social_media_links__has_key="instagram")
                .exclude(social_media_links__instagram="")
                .exclude(social_media_links__instagram__isnull=True)
                .values_list("businessId", flat=True)
            )
            for pk in pks:
                sync_instagram_followers_for_business.delay(pk)
            self.stdout.write(
                self.style.SUCCESS(f"Queued Instagram sync for {len(pks)} businesses")
            )
            return

        self.stderr.write("Specify --business-id N or --all")
