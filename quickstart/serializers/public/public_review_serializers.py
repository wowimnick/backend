from django.conf import settings
from rest_framework import serializers
from quickstart.models import CustomUser, Reviews


class ReviewSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for users submitting reviews."""

    image = serializers.ImageField(required=False, allow_null=True)
    booking_id = serializers.IntegerField(write_only=True, required=True)

    class Meta:
        model = Reviews
        fields = ["rating", "comment", "image", "booking_id"]

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
        resized_path = original_path.replace("originals/", "public/thumb/", 1)
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

    def _get_resized_image_url(self, obj, size_name):
        if not obj.image or not obj.image.name:
            return None
        original_path = obj.image.name
        if not original_path.startswith("originals/"):
            return None
        resized_path = original_path.replace("originals/", f"public/{size_name}/", 1)
        return f"{settings.CLOUDFRONT_DOMAIN}/{resized_path}"

    def get_image_thumb_url(self, obj):
        return self._get_resized_image_url(obj, "thumb")

    def get_image_medium_url(self, obj):
        return self._get_resized_image_url(obj, "medium")
