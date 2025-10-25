import json
import random
import time
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime
from django.db import transaction
from quickstart.models import BusinessInfo, ImportedGoogleReview
import logging
import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

# Configure logging
logger = logging.getLogger(__name__)


def download_image(url, max_retries=3):
    """
    Downloads an image from a URL with delays, a realistic user-agent, and a retry mechanism.
    Returns a ContentFile on success, None on failure.
    """
    # --- STRATEGY 1: BEHAVE LIKE A BROWSER ---
    # Send a common User-Agent header to appear as a standard web browser.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # --- STRATEGY 2: RETRIES WITH EXPONENTIAL BACKOFF ---
    # If a request fails, we'll wait and try again, waiting longer each time.
    wait_time = 1  # Start with a 1-second wait
    for attempt in range(max_retries):
        try:
            # --- STRATEGY 3: INTRODUCE DELAYS ---
            # Wait for a random duration between 0.5 and 2.0 seconds BEFORE each request.
            # This is the most critical step to avoid being flagged as a bot.
            time.sleep(random.uniform(0.5, 2.0))

            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()  # Raise an exception for HTTP errors (like 429 or 403)

            # If we get here, the download was successful
            return ContentFile(response.content)

        except requests.exceptions.RequestException as e:
            logger.warning(
                f"Attempt {attempt + 1}/{max_retries} failed to download image from {url}: {e}"
            )
            if attempt < max_retries - 1:
                logger.info(f"Waiting for {wait_time} seconds before retrying...")
                time.sleep(wait_time)
                wait_time *= 2  # Double the wait time for the next potential failure
            else:
                logger.error(
                    f"All {max_retries} attempts failed for URL: {url}. Skipping this image."
                )
                return None  # Final failure

    return None  # Should not be reached, but as a fallback


def get_filename_from_url(url, base_name):
    """
    Creates a simple filename from a URL and a base name.
    Example: "google_review_id_avatar.jpg"
    """
    # Use a simple .jpg extension as we're just storing the content.
    return f"{base_name}.jpg"


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

    @transaction.atomic
    def handle(self, *args, **options):
        business_id = options["business_id"]
        json_file_path = options["json_file"]
        skip_images = options.get("skip_images", False)

        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(self.style.SUCCESS("🚀 STARTING GOOGLE REVIEWS IMPORT"))
        self.stdout.write(f"{'='*80}")
        self.stdout.write(f"Business ID: {business_id}")
        self.stdout.write(f"JSON file: {json_file_path}")
        self.stdout.write(f"Skip images: {skip_images}")
        self.stdout.write(f"{'='*80}\n")

        try:
            business = BusinessInfo.objects.get(businessId=business_id)
            self.stdout.write(
                self.style.SUCCESS(f"✓ Found business: {business.businessName}")
            )
        except BusinessInfo.DoesNotExist:
            raise CommandError(f'Business with ID "{business_id}" does not exist.')

        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                reviews_data = json.load(f)
            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Loaded JSON file with {len(reviews_data)} reviews"
                )
            )
        except FileNotFoundError:
            raise CommandError(f'File not found at "{json_file_path}".')
        except json.JSONDecodeError:
            raise CommandError(f'Error decoding JSON from "{json_file_path}".')

        if not isinstance(reviews_data, list):
            raise CommandError("JSON file is not a list of review objects.")

        # Counters for the final report
        created_count = 0
        skipped_count = 0
        error_count = 0
        image_success_count = 0
        image_fail_count = 0

        self.stdout.write(f"\n{'─'*80}")
        self.stdout.write("📝 Processing reviews...")
        self.stdout.write(f"{'─'*80}\n")

        for idx, review_item in enumerate(reviews_data, 1):
            google_review_id = review_item.get("reviewId")

            self.stdout.write(
                f"\n[{idx}/{len(reviews_data)}] Processing review: {google_review_id}"
            )

            # Skip if there's no unique ID
            if not google_review_id:
                self.stdout.write(
                    self.style.WARNING(f"  ⚠️  Skipping - missing 'reviewId'")
                )
                skipped_count += 1
                continue

            # Skip reviews with no text content
            if not review_item.get("text"):
                self.stdout.write(self.style.NOTICE(f"  ⚠️  Skipping - no text content"))
                skipped_count += 1
                continue

            # Use get_or_create to avoid duplicating reviews
            try:
                review_instance, created = ImportedGoogleReview.objects.get_or_create(
                    google_review_id=google_review_id,
                    defaults={
                        "business": business,
                        "reviewer_name": review_item.get("name", "Anonymous"),
                        "rating": review_item.get("stars", 0),
                        "comment": review_item.get("text"),
                        "review_date": (
                            parse_datetime(review_item.get("publishedAtDate"))
                            if review_item.get("publishedAtDate")
                            else None
                        ),
                        "owner_response": review_item.get("responseFromOwnerText"),
                        "owner_response_date": (
                            parse_datetime(review_item.get("responseFromOwnerDate"))
                            if review_item.get("responseFromOwnerDate")
                            else None
                        ),
                        "image_urls": [],  # Default to empty list
                    },
                )

                if created:
                    self.stdout.write(self.style.SUCCESS(f"  ✓ Created new review"))
                    created_count += 1

                    if not skip_images:
                        # --- Download and save avatar ---
                        avatar_url = review_item.get("reviewerPhotoUrl")
                        if avatar_url:
                            self.stdout.write(f"  📥 Downloading avatar...")
                            avatar_content = download_image(avatar_url)
                            if avatar_content:
                                filename = get_filename_from_url(
                                    avatar_url, f"{google_review_id}_avatar"
                                )
                                review_instance.reviewer_avatar.save(
                                    filename, avatar_content, save=True
                                )
                                saved_path = review_instance.reviewer_avatar.name
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"     ✓ Saved avatar to: {saved_path}"
                                    )
                                )

                                # Verify path format
                                if not saved_path.startswith("originals/"):
                                    self.stdout.write(
                                        self.style.ERROR(
                                            f"     ✗ WARNING: Avatar path should start with 'originals/' but is: {saved_path}"
                                        )
                                    )

                                image_success_count += 1
                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"     ⚠️  Failed to download avatar"
                                    )
                                )
                                image_fail_count += 1
                        else:
                            self.stdout.write(f"  ℹ️  No avatar URL provided")

                        # --- Download and save review images ---
                        review_image_urls = review_item.get("reviewImageUrls", [])
                        if review_image_urls:
                            self.stdout.write(
                                f"  📥 Downloading {len(review_image_urls)} review image(s)..."
                            )
                            s3_image_keys = []

                            for i, image_url in enumerate(review_image_urls, 1):
                                self.stdout.write(
                                    f"     Image {i}/{len(review_image_urls)}..."
                                )
                                image_content = download_image(image_url)
                                if image_content:
                                    filename = get_filename_from_url(
                                        image_url, f"{google_review_id}_image_{i}"
                                    )
                                    # Save directly to storage and get the key/path
                                    s3_key = default_storage.save(
                                        f"originals/reviews/{filename}", image_content
                                    )
                                    s3_image_keys.append(s3_key)
                                    self.stdout.write(
                                        self.style.SUCCESS(
                                            f"        ✓ Saved to: {s3_key}"
                                        )
                                    )

                                    # Verify path format
                                    if not s3_key.startswith("originals/"):
                                        self.stdout.write(
                                            self.style.ERROR(
                                                f"        ✗ WARNING: Image path should start with 'originals/' but is: {s3_key}"
                                            )
                                        )

                                    image_success_count += 1
                                else:
                                    self.stdout.write(
                                        self.style.WARNING(
                                            f"        ⚠️  Failed to download"
                                        )
                                    )
                                    image_fail_count += 1

                            if s3_image_keys:
                                review_instance.image_urls = s3_image_keys
                                review_instance.save(update_fields=["image_urls"])
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"     ✓ Saved {len(s3_image_keys)} image path(s) to database"
                                    )
                                )
                        else:
                            self.stdout.write(f"  ℹ️  No review images")
                    else:
                        self.stdout.write(
                            self.style.NOTICE(
                                "  ⚠️  Skipping images (--skip-images flag)"
                            )
                        )

                else:
                    self.stdout.write(
                        self.style.NOTICE(f"  ⚠️  Already exists - skipping")
                    )
                    skipped_count += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Error: {e}"))
                logger.exception(f"Error processing review {google_review_id}")
                error_count += 1

        # Final report
        self.stdout.write(f"\n{'='*80}")
        self.stdout.write(self.style.SUCCESS("📊 IMPORT COMPLETE!"))
        self.stdout.write(f"{'='*80}")
        self.stdout.write(f"✅ Successfully created: {created_count} review(s)")
        self.stdout.write(
            f"⏭️  Skipped (already exist or invalid): {skipped_count} review(s)"
        )
        self.stdout.write(f"❌ Errors: {error_count} review(s)")

        if not skip_images:
            self.stdout.write(f"\n📷 Image Statistics:")
            self.stdout.write(
                f"✅ Successfully downloaded: {image_success_count} image(s)"
            )
            self.stdout.write(f"❌ Failed downloads: {image_fail_count} image(s)")

        self.stdout.write(f"\n💡 Next Steps:")
        self.stdout.write(f"1. Run debug command to verify image paths:")
        self.stdout.write(
            f"   python manage.py debug_review_images --business_id={business_id}"
        )
        self.stdout.write(f"2. Check if Lambda has processed the images:")
        self.stdout.write(
            f"   python manage.py debug_review_images --business_id={business_id} --check-s3"
        )
        self.stdout.write(f"3. View CloudWatch logs to see Lambda execution")
        self.stdout.write(f"{'='*80}\n")
