import os
from django.conf import settings
from rest_framework import serializers
from quickstart.models import CustomUser, ImportedGoogleReview, Reviews


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
            return None
        original_path = obj.avatar.name
        if not original_path.startswith("originals/"):
            return None
        # Remove original extension and add .webp
        base_path, _ = os.path.splitext(original_path)
        resized_path = base_path.replace("originals/", "public/thumb/", 1) + ".webp"
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"


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

            if not original_path.startswith("originals/"):
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
            return f"{settings.CLOUDFRONT_DOMAIN}/{final_path}"

        return None

    def get_image_thumb_url(self, obj):
        return self._get_resized_url(obj, "thumb")

    def get_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")


class ImportedGoogleReviewSerializer(serializers.ModelSerializer):
    """Serializer for displaying imported Google Reviews."""

    reviewer_avatar_url = serializers.ImageField(
        source="reviewer_avatar", read_only=True
    )
    image_urls = serializers.SerializerMethodField()

    class Meta:
        model = ImportedGoogleReview
        fields = [
            "google_review_id",
            "reviewer_name",
            "reviewer_avatar_url",
            "rating",
            "comment",
            "review_date",
            "owner_response",
            "image_urls",
            "source",
        ]

    def get_image_urls(self, obj):
        """
        Constructs full public URLs for the stored image keys.
        """
        if not obj.image_urls or not isinstance(obj.image_urls, list):
            return []

        # Use the default storage to get the public URL for each stored key.
        # This correctly handles S3/CloudFront domain configuration.
        from django.core.files.storage import default_storage

        return [default_storage.url(key) for key in obj.image_urls]
