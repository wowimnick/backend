import os
import logging
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from quickstart.models import CustomUser, ImportedGoogleReview, Reviews

logger = logging.getLogger(__name__)


class ReviewSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for users submitting reviews via S3 key."""

    image_s3_key = serializers.CharField(
        write_only=True, required=False, allow_null=True, allow_blank=True
    )
    booking_id = serializers.IntegerField(write_only=True, required=True)

    class Meta:
        model = Reviews
        fields = ["rating", "comment", "image_s3_key", "booking_id"]

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_comment(self, value):
        # Trim whitespace before checking length
        if len(value.strip()) < 10:
            raise serializers.ValidationError(
                "Comment must be at least 10 characters long."
            )
        return value.strip()  # Return the stripped comment

    def validate_booking_id(self, value):
        """Basic validation for booking_id format."""
        if not isinstance(value, int) or value <= 0:
            raise serializers.ValidationError("Invalid Booking ID provided.")
        # More detailed check (existence, ownership, status) happens in the view
        return value

    def validate_image_s3_key(self, value):
        """Validate the S3 key format."""
        if value and not value.startswith("originals/"):
            raise serializers.ValidationError(
                "Invalid S3 key provided for review image."
            )
        return value


class UserReviewSerializer(serializers.ModelSerializer):
    """Serializer for displaying basic user info attached to a public review."""

    name = serializers.SerializerMethodField()
    # Updated to provide a thumbnail URL for the avatar
    avatar_thumb_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ["name", "avatar_thumb_url"]

    def get_name(self, obj):
        if obj.first_name:
            last_initial = f" {obj.last_name[0]}." if obj.last_name else ""
            return f"{obj.first_name}{last_initial}"
        return "Anonymous User"

    def get_avatar_thumb_url(self, obj):
        if not obj.avatar or not obj.avatar.name:
            logger.debug(f"UserReviewSerializer: No avatar for user {obj.id}")
            return None
        original_path = obj.avatar.name
        logger.debug(f"UserReviewSerializer: Original avatar path: {original_path}")
        if not original_path.startswith("originals/"):
            logger.debug(
                f"UserReviewSerializer: Avatar not in originals/, returning None"
            )
            return None
        # Remove original extension and add .webp
        base_path, _ = os.path.splitext(original_path)
        resized_path = base_path.replace("originals/", "public/thumb/", 1) + ".webp"
        final_url = build_cloudfront_url(resized_path)
        logger.debug(f"UserReviewSerializer: Returning avatar URL: {final_url}")
        return final_url


class PublicReviewSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying reviews with optimized images."""

    user = UserReviewSerializer(source="userId", read_only=True)
    # Updated to provide multiple sizes for the review image
    image_thumb_url = serializers.SerializerMethodField()
    image_medium_url = serializers.SerializerMethodField()

    class Meta:
        model = Reviews
        fields = [
            "reviewId",
            "user",
            "rating",
            "comment",
            "image_thumb_url",
            "image_medium_url",
            "createdAt",
            "business_response",
        ]
        read_only_fields = fields

    def _get_resized_url(self, obj, size_name):
        """
        Constructs a public CloudFront URL for a resized WebP image.
        """
        if obj.image and obj.image.name:
            original_path = obj.image.name
            logger.debug(
                f"PublicReviewSerializer: Original image path: {original_path}, size: {size_name}"
            )

            if not original_path.startswith("originals/"):
                logger.debug(
                    f"PublicReviewSerializer: Image not in originals/, returning None"
                )
                return None

            # 1. Get the base path of the original image, without its extension
            base_path, _ = os.path.splitext(
                original_path
            )  # e.g., "originals/path/image.png" -> "originals/path/image"

            # 2. Replace the path prefix
            # e.g., "originals/path/image" -> "public/thumb/path/image"
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )

            # 3. Add the correct .webp extension
            final_path = resized_base_path + ".webp"

            # 4. Construct the full URL
            final_url = build_cloudfront_url(final_path)
            logger.info(
                f"PublicReviewSerializer: Returning {size_name} URL: {final_url}"
            )
            return final_url

        logger.debug(
            f"PublicReviewSerializer: No image found for review {obj.reviewId}"
        )
        return None

    def get_image_thumb_url(self, obj):
        return self._get_resized_url(obj, "thumb")

    def get_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")


class ImportedGoogleReviewSerializer(serializers.ModelSerializer):
    """Serializer for displaying imported Google Reviews."""

    business_name = serializers.CharField(
        source="business.businessName", read_only=True, allow_null=True
    )
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
