"""
Import / upsert Google reviews from Apify-shaped JSON into ImportedGoogleReview.
Shared by management command, admin upload, and Celery sync tasks.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Dict, List, Optional

import requests
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.dateparse import parse_datetime

from quickstart.models import BusinessInfo, ImportedGoogleReview

logger = logging.getLogger(__name__)

StreamWrite = Optional[Callable[[str], None]]


def download_image(url: str, max_retries: int = 3) -> Optional[ContentFile]:
    """Download an image from a URL with retries. Returns ContentFile on success."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
    }
    wait_time = 1
    for attempt in range(max_retries):
        try:
            time.sleep(random.uniform(0.5, 2.0))
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return ContentFile(response.content)
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Attempt %s/%s failed to download image from %s: %s",
                attempt + 1,
                max_retries,
                url,
                e,
            )
            if attempt < max_retries - 1:
                time.sleep(wait_time)
                wait_time *= 2
            else:
                logger.error("All %s attempts failed for URL: %s", max_retries, url)
                return None
    return None


def get_filename_from_url(url: str, base_name: str) -> str:
    return f"{base_name}.jpg"


def _clamp_rating(stars: Any) -> int:
    try:
        r = int(round(float(stars)))
    except (TypeError, ValueError):
        r = 0
    return max(1, min(5, r)) if r else 1


def import_reviews_for_business(
    business: BusinessInfo,
    reviews_data: List[Dict[str, Any]],
    *,
    skip_images: bool = False,
    full_refresh: bool = True,
    stream_write: StreamWrite = None,
) -> Dict[str, int]:
    """
    Upsert reviews for a business.

    - Skips rows without reviewId or without text (same as legacy CLI).
    - Images are downloaded only for newly created reviews.
    - If full_refresh, deletes ImportedGoogleReview rows for this business whose
      reviewId was not present anywhere in reviews_data.
    """
    if not isinstance(reviews_data, list):
        raise ValueError("reviews_data must be a list")

    def _say(msg: str) -> None:
        if stream_write:
            stream_write(msg)

    scraped_ids = {
        str(r.get("reviewId"))
        for r in reviews_data
        if r.get("reviewId") is not None and str(r.get("reviewId")).strip()
    }

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0
    image_success_count = 0
    image_fail_count = 0
    deleted_count = 0

    total_items = len(reviews_data)

    with transaction.atomic():
        for idx, review_item in enumerate(reviews_data, 1):
            google_review_id = review_item.get("reviewId")
            _say(f"\n[{idx}/{total_items}] Processing review: {google_review_id}")

            if not google_review_id:
                _say("  Skipping - missing 'reviewId'")
                skipped_count += 1
                continue

            gid = str(google_review_id).strip()
            if not gid:
                skipped_count += 1
                continue

            text = review_item.get("text")
            if not text:
                _say("  Skipping - no text content")
                skipped_count += 1
                continue

            rating = _clamp_rating(review_item.get("stars"))
            defaults = {
                "business": business,
                "reviewer_name": review_item.get("name", "Anonymous") or "Anonymous",
                "rating": rating,
                "comment": text,
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
            }

            try:
                try:
                    review_instance = ImportedGoogleReview.objects.get(
                        google_review_id=gid
                    )
                    created = False
                except ImportedGoogleReview.DoesNotExist:
                    review_instance = ImportedGoogleReview(
                        google_review_id=gid,
                        business=business,
                        image_urls=[],
                    )
                    created = True

                review_instance.business = business
                review_instance.reviewer_name = defaults["reviewer_name"]
                review_instance.rating = defaults["rating"]
                review_instance.comment = defaults["comment"]
                review_instance.review_date = defaults["review_date"]
                review_instance.owner_response = defaults["owner_response"]
                review_instance.owner_response_date = defaults[
                    "owner_response_date"
                ]

                if created:
                    review_instance.save()
                    created_count += 1
                    _say("  Created new review")

                    if not skip_images:
                        avatar_url = review_item.get("reviewerPhotoUrl")
                        if avatar_url:
                            _say("  Downloading avatar...")
                            avatar_content = download_image(avatar_url)
                            if avatar_content:
                                filename = get_filename_from_url(
                                    avatar_url, f"{gid}_avatar"
                                )
                                review_instance.reviewer_avatar.save(
                                    filename, avatar_content, save=True
                                )
                                image_success_count += 1
                            else:
                                image_fail_count += 1
                        review_image_urls = review_item.get("reviewImageUrls") or []
                        if review_image_urls:
                            _say(
                                f"  Downloading {len(review_image_urls)} review image(s)..."
                            )
                            s3_image_keys: List[str] = []
                            for i, image_url in enumerate(review_image_urls, 1):
                                image_content = download_image(image_url)
                                if image_content:
                                    filename = get_filename_from_url(
                                        image_url, f"{gid}_image_{i}"
                                    )
                                    s3_key = default_storage.save(
                                        f"originals/reviews/{filename}",
                                        image_content,
                                    )
                                    s3_image_keys.append(s3_key)
                                    image_success_count += 1
                                else:
                                    image_fail_count += 1
                            if s3_image_keys:
                                review_instance.image_urls = s3_image_keys
                                review_instance.save(update_fields=["image_urls"])
                    else:
                        _say("  Skipping images (skip_images=True)")
                else:
                    review_instance.save()
                    updated_count += 1
                    _say("  Updated existing review")

            except Exception as e:
                _say(f"  Error: {e}")
                logger.exception("Error processing review %s", google_review_id)
                error_count += 1

        if full_refresh and scraped_ids:
            qs = ImportedGoogleReview.objects.filter(business=business).exclude(
                google_review_id__in=scraped_ids
            )
            deleted_count = qs.count()
            qs.delete()

    return {
        "created": created_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "errors": error_count,
        "deleted": deleted_count,
        "image_success": image_success_count,
        "image_fail": image_fail_count,
    }
