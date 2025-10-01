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

    @transaction.atomic
    def handle(self, *args, **options):
        business_id = options["business_id"]
        json_file_path = options["json_file"]

        self.stdout.write(
            f"Starting import process for business ID: {business_id} from file: {json_file_path}"
        )

        try:
            business = BusinessInfo.objects.get(businessId=business_id)
        except BusinessInfo.DoesNotExist:
            raise CommandError(f'Business with ID "{business_id}" does not exist.')

        try:
            with open(json_file_path, "r", encoding="utf-8") as f:
                reviews_data = json.load(f)
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

        for review_item in reviews_data:
            google_review_id = review_item.get("reviewId")

            # Skip if there's no unique ID
            if not google_review_id:
                self.stderr.write(
                    self.style.WARNING(f"Skipping a review due to missing 'reviewId'.")
                )
                skipped_count += 1
                continue

            # Skip reviews with no text content
            if not review_item.get("text"):
                self.stdout.write(
                    self.style.NOTICE(
                        f"Skipping review {google_review_id} because it has no text content."
                    )
                )
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
                    created_count += 1

                    # --- Download and save avatar ---
                    avatar_url = review_item.get("reviewerPhotoUrl")
                    if avatar_url:
                        avatar_content = download_image(avatar_url)
                        if avatar_content:
                            filename = get_filename_from_url(
                                avatar_url, f"{google_review_id}_avatar"
                            )
                            review_instance.reviewer_avatar.save(
                                filename, avatar_content, save=True
                            )

                    # --- Download and save review images ---
                    s3_image_keys = []
                    review_image_urls = review_item.get("reviewImageUrls", [])
                    for i, image_url in enumerate(review_image_urls):
                        image_content = download_image(image_url)
                        if image_content:
                            filename = get_filename_from_url(
                                image_url, f"{google_review_id}_image_{i}"
                            )
                            # Save directly to storage and get the key/path
                            s3_key = default_storage.save(
                                f"public/reviews/{filename}", image_content
                            )
                            s3_image_keys.append(s3_key)

                    if s3_image_keys:
                        review_instance.image_urls = s3_image_keys
                        review_instance.save(update_fields=["image_urls"])

                else:
                    skipped_count += 1  # Already exists
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"Error processing review {google_review_id}: {e}")
                )
                error_count += 1

        self.stdout.write(self.style.SUCCESS("--------------------"))
        self.stdout.write(self.style.SUCCESS("Import process complete!"))
        self.stdout.write(f"Successfully created: {created_count} reviews.")
        self.stdout.write(
            f"Skipped (already exist or invalid): {skipped_count} reviews."
        )
        self.stdout.write(f"Errors: {error_count} reviews.")
