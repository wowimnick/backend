from rest_framework import serializers
from ...models import CustomUser, Reviews

class ReviewSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for users submitting reviews."""
    image = serializers.ImageField(required=False, allow_null=True)
    booking_id = serializers.IntegerField(write_only=True, required=True)

    class Meta:
        model = Reviews
        fields = ['rating', 'comment', 'image', 'booking_id']

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_comment(self, value):
        # Trim whitespace before checking length
        if len(value.strip()) < 10:
            raise serializers.ValidationError("Comment must be at least 10 characters long.")
        return value.strip() # Return the stripped comment

    def validate_booking_id(self, value):
        """Basic validation for booking_id format."""
        if not isinstance(value, int) or value <= 0:
            raise serializers.ValidationError("Invalid Booking ID provided.")
        # More detailed check (existence, ownership, status) happens in the view
        return value

class UserReviewSerializer(serializers.ModelSerializer):
    """Serializer for displaying basic user info attached to a public review."""
    name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['name', 'avatar_url'] # Only public-safe fields

    def get_name(self, obj):
        # Display first name and last initial for privacy
        if obj.first_name:
            last_initial = f" {obj.last_name[0]}." if obj.last_name else ""
            return f"{obj.first_name}{last_initial}"
        # Fallback if no first name (shouldn't happen often with required fields)
        return "Anonymous User"

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
            try:
                return obj.avatar.url
            except ValueError: # Handle file missing
                 return None
        return None # Default no avatar

class PublicReviewSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying reviews (e.g., on a class page)."""
    user = UserReviewSerializer(source='userId', read_only=True) # Use the privacy-conscious user serializer
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Reviews
        # Fields safe for public display
        fields = [
            'reviewId',
            'user', # Nested user info
            'rating',
            'comment',
            'image_url', # URL for the image
            'createdAt',
            'business_response', # Make the business response public
        ]
        read_only_fields = fields # All fields read-only in this context

    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            try:
                return obj.image.url
            except ValueError:
                return None
        return None