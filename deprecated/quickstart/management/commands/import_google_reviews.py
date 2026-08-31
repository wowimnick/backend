import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from quickstart.models import BusinessInfo
from quickstart.services.google_reviews_importer import import_reviews_for_business


class Command(BaseCommand):
    help = "Imports Google reviews from a JSON file for a specified business."

    def add_arguments(self, parser):
        parser.add_argument(
            "business_id",
            type=int,
            help="The ID of the business to associate the reviews with.",
        )
        parser.add_argument(
            "json_file",
            type=str,
            help="The path to the JSON file containing the reviews.",
        )
        parser.add_argument(
            "--skip-images",
            action="store_true",
            help="Skip downloading images (for testing)",
        )
        parser.add_argument(
            "--no-full-refresh",
            action="store_true",
            help="Do not delete reviews missing from this JSON file (legacy one-way import).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        business_id = options["business_id"]
        json_file_path = options["json_file"]
        skip_images = options.get("skip_images", False)
        full_refresh = not options.get("no_full_refresh", False)

        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(self.style.SUCCESS("STARTING GOOGLE REVIEWS IMPORT"))
        self.stdout.write(f"{'='*80}")
        self.stdout.write(f"Business ID: {business_id}")
        self.stdout.write(f"JSON file: {json_file_path}")
        self.stdout.write(f"Skip images: {skip_images}")
        self.stdout.write(f"Full refresh (delete orphans): {full_refresh}")
        self.stdout.write(f"{'='*80}\n")

        try:
            business = BusinessInfo.objects.get(businessId=business_id)
            self.stdout.write(
                self.style.SUCCESS(f"Found business: {business.businessName}")
            )
        except BusinessInfo.DoesNotExist:
            raise CommandError(f'Business with ID "{business_id}" does not exist.')

        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                reviews_data = json.load(f)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Loaded JSON file with {len(reviews_data)} top-level items"
                )
            )
        except FileNotFoundError:
            raise CommandError(f'File not found at "{json_file_path}".')
        except json.JSONDecodeError:
            raise CommandError(f'Error decoding JSON from "{json_file_path}".')

        if not isinstance(reviews_data, list):
            raise CommandError("JSON file is not a list of review objects.")

        self.stdout.write(f"\n{'-'*80}")
        self.stdout.write("Processing reviews...")
        self.stdout.write(f"{'-'*80}\n")

        stats = import_reviews_for_business(
            business,
            reviews_data,
            skip_images=skip_images,
            full_refresh=full_refresh,
            stream_write=lambda m: self.stdout.write(m),
        )

        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(self.style.SUCCESS("IMPORT COMPLETE!"))
        self.stdout.write(f"{'='*80}")
        self.stdout.write(f"Created: {stats['created']} review(s)")
        self.stdout.write(f"Updated: {stats['updated']} review(s)")
        self.stdout.write(f"Deleted (orphans): {stats['deleted']} review(s)")
        self.stdout.write(
            f"Skipped (missing id / no text / invalid): {stats['skipped']} review(s)"
        )
        self.stdout.write(f"Errors: {stats['errors']} review(s)")

        if not skip_images:
            self.stdout.write(f"\nImage statistics:")
            self.stdout.write(
                f"Successfully downloaded: {stats['image_success']} image(s)"
            )
            self.stdout.write(f"Failed downloads: {stats['image_fail']} image(s)")

        self.stdout.write(f"\nNext steps:")
        self.stdout.write(
            f"  python manage.py debug_review_images --business_id={business_id}"
        )
        self.stdout.write(
            f"  python manage.py debug_review_images --business_id={business_id} --check-s3"
        )
        self.stdout.write(f"{'='*80}\n")
