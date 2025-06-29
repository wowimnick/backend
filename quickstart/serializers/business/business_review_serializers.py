from rest_framework import serializers
from quickstart.models import Reviews, CustomUser, Booking, ClassesMain


class BusinessReviewUserSerializer(serializers.ModelSerializer):
    """Serializer for user details within a business review context."""

    class Meta:
        model = CustomUser
        fields = [
            "userId",
            "first_name",
            "last_name",
            "email",
            "avatar",
        ]  # Added avatar
        read_only_fields = fields


class BusinessReviewBookingSerializer(serializers.ModelSerializer):
    """Serializer for booking details within a business review context."""

    class Meta:
        model = Booking
        fields = ["id", "booking_date"]  # Simplified
        read_only_fields = fields


class BusinessReviewClassSerializer(serializers.ModelSerializer):
    """Serializer for class details within a business review."""

    class Meta:
        model = ClassesMain
        fields = ["classId", "title"]
        read_only_fields = fields


class BusinessReviewSerializer(serializers.ModelSerializer):
    """Serializer for businesses viewing and managing reviews."""

    user = BusinessReviewUserSerializer(source="userId", read_only=True)
    booking = BusinessReviewBookingSerializer(read_only=True)
    class_info = BusinessReviewClassSerializer(source="classId", read_only=True)
    image_url = serializers.ImageField(source="image", read_only=True, use_url=True)

    class Meta:
        model = Reviews
        fields = [
            "reviewId",
            "user",
            "booking",
            "class_info",
            "rating",
            "comment",
            "image_url",
            "status",
            "reported",
            "report_reason",  # Read-only for business user when viewing
            "business_response",
            "responded_at",
            "reported_at",
            "createdAt",
        ]
        read_only_fields = [
            "reviewId",
            "user",
            "booking",
            "class_info",
            "rating",
            "comment",
            "image_url",
            "status",
            "reported",
            "report_reason",  # report_reason is set via action, not direct update
            "responded_at",
            "reported_at",
            "createdAt",
        ]
        extra_kwargs = {
            # business_response is writable for the 'respond' action
            "business_response": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
        }
