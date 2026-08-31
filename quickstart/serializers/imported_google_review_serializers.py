import os
import logging
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from quickstart.models import ImportedGoogleReview

logger = logging.getLogger(__name__)

class ImportedGoogleReviewSerializer(serializers.ModelSerializer):
    """Serializer for displaying imported Google Reviews."""

    business_name = serializers.SerializerMethodField()
    # FIXED: Changed field names to match frontend expectations
    reviewer_avatar_url = (
        serializers.SerializerMethodField()
    )  # was reviewer_avatar_thumb_url
    image_urls = (
        serializers.SerializerMethodField()
    )  # Primary field - returns medium URLs

    # Keep these for backward compatibility
    image_thumb_urls = serializers.SerializerMethodField()
    image_medium_urls = serializers.SerializerMethodField()

    class Meta:
        model = ImportedGoogleReview
        fields = [
            "google_review_id",
            "reviewer_name",
            "reviewer_avatar_url",  # CHANGED to match frontend
            "rating",
            "comment",
            "review_date",
            "owner_response",
            "business_name",
            "image_urls",  # CHANGED - primary field
            "image_thumb_urls",
            "image_medium_urls",
            "source",
        ]

    def get_business_name(self, obj):
        if "business_name" in self.context:
            return self.context["business_name"]
        business = getattr(obj, "business", None)
        return business.businessName if business else None

    def get_reviewer_avatar_url(self, obj):
        """
        Returns the thumbnail WebP URL for the reviewer avatar.
        RENAMED from get_reviewer_avatar_thumb_url to match frontend expectations.
        """
        logger.debug(
            f"ImportedGoogleReviewSerializer: Processing avatar for review {obj.google_review_id}"
        )

        if not obj.reviewer_avatar or not obj.reviewer_avatar.name:
            logger.debug(
                f"ImportedGoogleReviewSerializer: No avatar for review {obj.google_review_id}"
            )
            return None

        original_path = obj.reviewer_avatar.name
        logger.debug(
            f"ImportedGoogleReviewSerializer: Original avatar path: {original_path}"
        )

        # If it's already in public/, return as-is (legacy data)
        if original_path.startswith("public/"):
            final_url = build_cloudfront_url(original_path)
            logger.debug(
                f"ImportedGoogleReviewSerializer: Legacy avatar, returning: {final_url}"
            )
            return final_url

        # If it's in originals/, convert to thumb WebP
        if original_path.startswith("originals/"):
            base_path, _ = os.path.splitext(original_path)
            thumb_path = base_path.replace("originals/", "public/thumb/", 1) + ".webp"
            final_url = build_cloudfront_url(thumb_path)
            logger.debug(
                f"ImportedGoogleReviewSerializer: Converted avatar to: {final_url}"
            )
            return final_url

        logger.debug(
            f"ImportedGoogleReviewSerializer: Avatar path doesn't start with public/ or originals/: {original_path}"
        )
        return None

    def _convert_to_processed_urls(self, image_keys, size_name):
        """
        Converts originals/ paths to public/SIZE/ WebP URLs.
        """
        logger.debug(
            f"ImportedGoogleReviewSerializer: Converting {len(image_keys) if image_keys else 0} images to {size_name}"
        )

        if not image_keys or not isinstance(image_keys, list):
            logger.debug(f"ImportedGoogleReviewSerializer: No image_keys or not a list")
            return []

        processed_urls = []
        for idx, key in enumerate(image_keys):
            logger.debug(
                f"ImportedGoogleReviewSerializer: Processing image {idx}: {key}"
            )
            if key.startswith("originals/"):
                base_path, _ = os.path.splitext(key)
                processed_path = (
                    base_path.replace("originals/", f"public/{size_name}/", 1) + ".webp"
                )
                final_url = build_cloudfront_url(processed_path)
                logger.debug(
                    f"ImportedGoogleReviewSerializer: Converted image {idx} to: {final_url}"
                )
                processed_urls.append(final_url)
            else:
                logger.debug(
                    f"ImportedGoogleReviewSerializer: Image {idx} doesn't start with originals/: {key}"
                )

        logger.debug(
            f"ImportedGoogleReviewSerializer: Returning {len(processed_urls)} {size_name} URLs"
        )
        return processed_urls

    def get_image_urls(self, obj):
        """
        Returns medium WebP URLs for review images.
        PRIMARY FIELD - Frontend expects 'image_urls' as the main array.
        """
        logger.debug(
            f"ImportedGoogleReviewSerializer: get_image_urls (PRIMARY) called for review {obj.google_review_id}"
        )
        return self._convert_to_processed_urls(obj.image_urls, "medium")

    def get_image_thumb_urls(self, obj):
        """
        Returns thumbnail WebP URLs for review images.
        """
        logger.debug(
            f"ImportedGoogleReviewSerializer: get_image_thumb_urls called for review {obj.google_review_id}"
        )
        return self._convert_to_processed_urls(obj.image_urls, "thumb")

    def get_image_medium_urls(self, obj):
        """
        Returns medium WebP URLs for review images.
        """
        logger.debug(
            f"ImportedGoogleReviewSerializer: get_image_medium_urls called for review {obj.google_review_id}"
        )
        return self._convert_to_processed_urls(obj.image_urls, "medium")


