from rest_framework import serializers
from ..utils.permissions import check_user_role
from ..models import CustomUser, Reviews 

class ReviewSubmissionSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=False)
    
    class Meta:
        model = Reviews
        fields = ['rating', 'comment', 'image']
        
    def validate(self, data):
        if data['rating'] < 1 or data['rating'] > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5")
        
        if len(data['comment'].strip()) < 10:
            raise serializers.ValidationError("Review comment must be at least 10 characters")
            
        return data
    
class UserReviewSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomUser
        fields = ['name', 'avatar_url']
    
    def get_name(self, obj):
        if obj.first_name:
            last_initial = f" {obj.last_name[0]}." if obj.last_name else ""
            return f"{obj.first_name}{last_initial}"
        return "Anonymous"
    
    def get_avatar_url(self, obj):
        if obj.avatar:
            return obj.avatar.url
        return None

class ReviewSerializer(serializers.ModelSerializer):
    user = UserReviewSerializer(source='userId', read_only=True)
    image_url = serializers.SerializerMethodField()
    booking_info = serializers.SerializerMethodField()

    class Meta:
        model = Reviews
        fields = [
            'reviewId',
            'user',
            'rating',
            'comment',
            'image_url',
            'createdAt',
            'booking_info'
        ]
    
    def get_image_url(self, obj):
        if obj.image:
            return obj.image.url
        return None
        
    def get_booking_info(self, obj):
        """Return booking information if available"""
        if obj.booking:
            return {
                'booking_id': obj.booking.id,
                'booking_date': obj.booking.booking_date,
                'status': obj.booking.status
            }
        return None