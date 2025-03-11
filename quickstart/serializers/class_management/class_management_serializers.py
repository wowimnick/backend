from rest_framework import serializers
from ...models import (
    ClassCategory, ClassSubcategory, ClassesMain, ClassImage, ClassOption, Reviews, 
    Schedule, ScheduleInstance, BusinessInfo
)
from ...serializers.review_serializers import ReviewSerializer

class AdminScheduleInstanceSerializer(serializers.ModelSerializer):
    booking_count = serializers.IntegerField(source='current_bookings', read_only=True)
    available_spots = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = ScheduleInstance
        fields = [
            'id', 'date', 'time', 'duration', 'price',
            'max_participants', 'status', 'booking_count',
            'available_spots', 'attendance_marked',
            'instructor_notes', 'cancellation_reason',
            'created_at', 'updated_at'
        ]

class AdminScheduleSerializer(serializers.ModelSerializer):
    instances = AdminScheduleInstanceSerializer(many=True, read_only=True)
    
    class Meta:
        model = Schedule
        fields = [
            'id', 'day', 'time', 'duration', 'price',
            'maxParticipants', 'start_date', 'end_date',
            'date', 'is_active', 'allow_late_enrollment',
            'instances'
        ]

class AdminClassOptionSerializer(serializers.ModelSerializer):
    schedules = AdminScheduleSerializer(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()
    price_range = serializers.SerializerMethodField()
    total_students = serializers.IntegerField(read_only=True, default=0)
    
    class Meta:
        model = ClassOption
        fields = [
            'optionId', 'title', 'description',
            'booking_type', 'level',
            'price_type', 'equipment', 'tags',
            'cancellationPolicy', 'schedules',
            'image', 'image_url', 'price_range',
            'total_students', 'createdAt', 'updatedAt'
        ]
    
    def get_image_url(self, obj):
        if obj.image:
            return obj.image.url
        return None
    
    def get_price_range(self, obj):
        """Calculate price range across all schedules"""
        schedules = obj.schedules.all()
        if not schedules.exists():
            return None
        
        prices = [schedule.price for schedule in schedules]
        min_price = min(prices) if prices else None
        max_price = max(prices) if prices else None
        
        if min_price is None:
            return None
        
        if min_price == max_price:
            return {'min': min_price, 'max': min_price, 'single_price': True}
        
        return {'min': min_price, 'max': max_price, 'single_price': False}

class AdminClassImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

class AdminBusinessSerializer(serializers.ModelSerializer):
    """Simplified business serializer for admin use"""
    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'businessType',
            'businessImage', 'businessCity', 'businessState',
            'verificationStatus', 'isActive', 'featured'
        ]

class AdminClassSerializer(serializers.ModelSerializer):
    """
    Serializer for class list view in admin panel
    """
    images = AdminClassImageSerializer(many=True, read_only=True)
    business = AdminBusinessSerializer(source='businessId', read_only=True)
    price_range = serializers.SerializerMethodField()
    category_display = serializers.SerializerMethodField()
    active_classes = serializers.IntegerField(source='active_schedules_count', read_only=True)
    featured = serializers.BooleanField(source='businessId.featured', read_only=True)
    
    # The average_rating field comes from the annotated queryset in the view
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = ClassesMain
        fields = [
            'classId', 'title', 'description',
            'category', 'category_display', 'subcategory',
            'location', 'coordinates', 'price_range',
            'status', 'active_classes', 'featured',
            'images', 'business', 'average_rating', 'review_count',
            'createdAt', 'updatedAt'
        ]
    
    def get_price_range(self, obj):
        """Get price range across all options and schedules"""
        min_price = getattr(obj, 'min_price', None)
        max_price = getattr(obj, 'max_price', None)
        
        if min_price is None:
            return None
        
        if min_price == max_price:
            return {'min': min_price, 'max': min_price, 'single_price': True}
        
        return {'min': min_price, 'max': max_price, 'single_price': False}
    
    def get_category_display(self, obj):
        """Return formatted category name"""
        if not obj.category:
            return "Uncategorized"
        return obj.category.name.capitalize()
    
    def to_representation(self, instance):
        """
        Override to customize the serialized output by adding additional category
        and subcategory information for the frontend
        """
        data = super().to_representation(instance)
        
        # Add category info if available
        if instance.category:
            data['category'] = {
                'id': instance.category.id,
                'name': instance.category.name,
                'key': instance.category.key,
                'color': instance.category.color
            }
        
        # Add subcategory info if available
        if instance.subcategory:
            data['subcategory'] = {
                'id': instance.subcategory.id,
                'name': instance.subcategory.name,
                'key': instance.subcategory.key,
                'description': instance.subcategory.description
            }
        
        return data

class AdminClassDetailSerializer(AdminClassSerializer):
    """
    Detailed serializer for single class view in admin panel
    """
    options = AdminClassOptionSerializer(many=True, read_only=True)
    reviews = serializers.SerializerMethodField()
    
    class Meta(AdminClassSerializer.Meta):
        fields = AdminClassSerializer.Meta.fields + ['options', 'reviews']
    
    def get_reviews(self, obj):
        """Get recent reviews (limit to 5)"""
        # In a real implementation, you might want pagination for all reviews
        reviews = obj.reviews.all().order_by('-createdAt')[:5]
        return ReviewSerializer(reviews, many=True).data

class AdminClassCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating classes through admin panel
    """
    class Meta:
        model = ClassesMain
        fields = [
            'title', 'description', 'category', 'subcategory',
            'location', 'coordinates', 'status', 
            'studentContactEmail', 'studentContactPhone'
        ]

class SubcategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassSubcategory
        fields = ['id', 'name', 'key', 'description']
        
class AdminClassCategorySerializer(serializers.ModelSerializer):
    """
    Serializer for category management
    """
    class Meta:
        model = ClassCategory
        fields = ['id', 'name', 'key', 'color', 'created_at', 'updated_at']
    
class AdminReviewSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    className = serializers.CharField(source='classId.title')
    businessName = serializers.CharField(source='businessId.businessName')
    
    class Meta:
        model = Reviews
        fields = [
            'reviewId', 'userId', 'user', 'classId', 'className',
            'businessId', 'businessName', 'rating', 'comment',
            'status', 'reported', 'report_reason', 'business_response',
            'createdAt'
        ]
    
    def get_user(self, obj):
        return {
            'name': obj.userId.get_full_name() or obj.userId.username,
            'avatar_url': obj.userId.get_avatar_url()
        }