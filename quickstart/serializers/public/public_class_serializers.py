# serializers/classes/public_class_serializers.py
from rest_framework import serializers
from ...models import ClassesMain, ClassImage, ClassOption, Schedule, Reviews
from django.utils import timezone
from django.db.models import Avg
import logging
from random import uniform # For coordinate salting if needed here

logger = logging.getLogger(__name__)

class PublicClassImageSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying class images."""
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image'] # Only show ID and image URL

class PublicScheduleSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying basic schedule info."""
    class Meta:
        model = Schedule
        fields = ['id', 'day', 'time', 'duration', 'price', 'maxParticipants', 'start_date', 'end_date', 'date', 'allow_late_enrollment'] 
        read_only_fields = fields 

class PublicClassOptionSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying class options."""
    schedules = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassOption
        fields = [
            'optionId',
            'title', 'description',
            'booking_type', 'level',
            'equipment',
            'tags',
            'cancellationPolicy',
            'image_url',
            'schedules', # This will now include price/capacity
            'price_type', # Include if relevant for public display
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
            try:
                 return obj.image.url
            except ValueError:
                 return None
        return None
    
    def get_schedules(self, option_instance: ClassOption):
        """
        Returns only active and future schedules for the given class option.
        """
        today = timezone.now().date()
        
        # Access schedules related to the option_instance.
        # This relies on proper prefetching in the ViewSet to be efficient.
        # The prefetch should ensure that option_instance.schedules.all()
        # returns only schedules where `is_active=True`.
        active_schedules = option_instance.schedules.all()

        future_schedules_objects = []
        for schedule_obj in active_schedules: # Iterate over actual Schedule model instances
            is_future = False
            if option_instance.booking_type == 'Full Course':
                # For courses, relevant if its end_date is today or in the future.
                if schedule_obj.end_date and schedule_obj.end_date >= today:
                    is_future = True
            else: # Single Session
                # For single sessions, relevant if its date is today or in the future.
                if schedule_obj.date and schedule_obj.date >= today:
                    is_future = True
            
            if is_future:
                future_schedules_objects.append(schedule_obj)
        
        # Serialize only the filtered future schedules
        return PublicScheduleSerializer(future_schedules_objects, many=True, context=self.context).data

class PublicClassSerializer(serializers.ModelSerializer):
    """Serializer for public listing and detail view of classes."""
    options = PublicClassOptionSerializer(many=True, read_only=True)
    images = PublicClassImageSerializer(many=True, read_only=True) # <--- ADD THIS LINE
    business_name = serializers.CharField(source='businessId.businessName', read_only=True)
    business_image = serializers.SerializerMethodField(read_only=True)
    coordinates = serializers.SerializerMethodField(read_only=True)
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)
    featured = serializers.BooleanField(source='businessId.featured', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    subcategory_name = serializers.CharField(source='subcategory.name', read_only=True, allow_null=True)
    saltLocation = serializers.BooleanField(read_only=True)
    is_favorited = serializers.SerializerMethodField()

    class Meta:
        model = ClassesMain
        fields = [
            'classId', 'businessId', 'title', 'description',
            'features',
            'category_name',
            'subcategory_name',
            'coordinates',
            'studentContactEmail',
            'studentContactPhone',
            'createdAt',
            'business_name',
            'business_image',
            'options',
            'images',  
            'average_rating',
            'review_count',
            'featured',
            'saltLocation',
            'is_favorited',
        ]
        read_only_fields = fields

    def get_business_image(self, obj):
        if obj.businessId and hasattr(obj.businessId, 'businessImage') and obj.businessId.businessImage:
            try:
                return obj.businessId.businessImage.url
            except ValueError: return None
        return None

    def get_coordinates(self, obj):
        if not obj.coordinates: return None
        try:
            lat, lng = map(float, obj.coordinates.split(','))
            if obj.saltLocation:
                lat_salt = uniform(-0.0005, 0.0005)
                lng_salt = uniform(-0.0005, 0.0005)
                lat += lat_salt
                lng += lng_salt
            return f"{lat:.8f},{lng:.8f}"
        except (ValueError, TypeError):
             logger.warning(f"Invalid public coordinates format for Class {obj.classId}: {obj.coordinates}")
             return None

    def get_is_favorited(self, obj):
        request = self.context.get('request')
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            return request.user.favorited.filter(pk=obj.pk).exists()
        return False