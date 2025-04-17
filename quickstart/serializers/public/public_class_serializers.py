# serializers/classes/public_class_serializers.py
from rest_framework import serializers
from ...models import ClassesMain, ClassImage, ClassOption, Schedule, Reviews
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
    schedules = PublicScheduleSerializer(many=True, read_only=True)
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

class PublicClassSerializer(serializers.ModelSerializer):
    """Serializer for public listing and detail view of classes."""
    options = PublicClassOptionSerializer(many=True, read_only=True)
    business_name = serializers.CharField(source='businessId.businessName', read_only=True)
    business_image = serializers.SerializerMethodField(read_only=True)
    coordinates = serializers.SerializerMethodField(read_only=True) # Handles salting
    average_rating = serializers.FloatField(read_only=True) # Assumes annotated in view
    review_count = serializers.IntegerField(read_only=True) # Assumes annotated in view
    featured = serializers.BooleanField(source='businessId.featured', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    subcategory_name = serializers.CharField(source='subcategory.name', read_only=True, allow_null=True)
    saltLocation = serializers.BooleanField(read_only=True)

    class Meta:
        model = ClassesMain
        # Explicitly list fields safe for public view
        fields = [
            'classId', 'businessId', 'title', 'description',
            'features', 
            'category_name',
            'subcategory_name',
            'coordinates', # Salted coordinates from method
            'studentContactEmail', 
            'studentContactPhone', 
            'createdAt',
            'business_name',
            'business_image',
            'options',
            'average_rating',
            'review_count',
            'featured',
            'saltLocation',
        ]
        read_only_fields = fields

    def get_business_image(self, obj):
        if obj.businessId and hasattr(obj.businessId, 'businessImage') and obj.businessId.businessImage:
            try:
                return obj.businessId.businessImage.url
            except ValueError: return None
        return None

    def get_coordinates(self, obj):
        # Keep existing salting logic from original serializer
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