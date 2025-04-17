from rest_framework import serializers
from ...models import Reviews, CustomUser, Booking
from ..public.public_review_serializers import PublicReviewSerializer # Can inherit if useful

class BusinessReviewUserSerializer(serializers.ModelSerializer):
    """Serializer for user details within a business review context."""
    # Show more details than public view if needed, respecting privacy policies
    class Meta:
        model = CustomUser
        fields = ['userId', 'first_name', 'last_name', 'email'] # Example: Show email to business

class BusinessReviewBookingSerializer(serializers.ModelSerializer):
     """Serializer for booking details within a business review context."""
     class Meta:
         model = Booking
         fields = ['id', 'booking_date', 'status'] # Example fields

class BusinessReviewSerializer(serializers.ModelSerializer):
    """Serializer for businesses viewing and managing reviews."""
    user = BusinessReviewUserSerializer(source='userId', read_only=True)
    booking = BusinessReviewBookingSerializer(read_only=True) # Show linked booking info
    image_url = serializers.SerializerMethodField(read_only=True)
    # Include fields relevant for business management
    # business_response is editable

    class Meta:
        model = Reviews
        fields = [
            'reviewId',
            'user',         # More detailed user info
            'booking',      # Linked booking info
            'rating',
            'comment',
            'image_url',
            'status',       # Show current status (read-only for business)
            'reported',     # Show if reported (read-only for business)
            'report_reason',# Show report reason (read-only for business)
            'business_response', # Editable by business
            'createdAt',
        ]
        read_only_fields = [
            'reviewId', 'user', 'booking', 'rating', 'comment', 'image_url',
            'status', 'reported', 'report_reason', 'createdAt'
            # 'business_response' is writable
        ]
        extra_kwargs = {
            'business_response': {'required': False, 'allow_blank': True} # Allow submitting empty response
        }

    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            try:
                return obj.image.url
            except ValueError:
                return None
        return None