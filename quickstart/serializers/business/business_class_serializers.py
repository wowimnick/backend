# serializers/classes/business_class_serializers.py
from rest_framework import serializers
from django.db import transaction
from django.db.models import Q
import json
import logging

# Adjust import paths as needed
from ...models import (
    BusinessInfo, ClassCategory, ClassSubcategory, ClassesMain, ClassImage,
    ClassOption, Schedule, ScheduleInstance
)
# Assuming business permissions are defined elsewhere
# from ...permissions import check_user_role # If needed for validation

logger = logging.getLogger(__name__)

# --- Serializers primarily used in Business Management Context ---

class ClassImageSerializer(serializers.ModelSerializer):
    """Serializer for managing class images (upload/delete)."""
    # image = serializers.ImageField(required=True) # Already defined in model
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']
        read_only_fields = ['imageId', 'createdAt']

class ScheduleInstanceSerializer(serializers.ModelSerializer):
    """Serializer for managing schedule instances (e.g., cancel, mark attendance)."""
    # Add read-only fields calculated from annotations if needed
    current_bookings_count = serializers.IntegerField(read_only=True)
    available_spots = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ScheduleInstance
        fields = [
            'id', 'schedule', 'date', 'time', 'duration', 'price',
            'max_participants', 'status', 'cancellation_reason',
            'instructor_notes', 'attendance_marked',
            'current_bookings_count', 'available_spots',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'schedule', 'created_at', 'updated_at', 'current_bookings_count', 'available_spots']
        # Make fields like status, reason, notes updatable via specific actions

    def get_available_spots(self, obj):
        # Calculation based on annotation or property
        current_bookings = getattr(obj, 'current_bookings_count', 0)
        return obj.max_participants - current_bookings

class ScheduleSerializer(serializers.ModelSerializer):
    """Serializer for creating/managing schedules within a class option."""

    class Meta:
        model = Schedule
        fields = [
            'id', 'option', 'day', 'time', 'duration',
            'price', 'maxParticipants', # Renamed from max_participants if model was updated
            'is_active',
            'start_date', 'end_date', # For courses
            'date', # For single sessions
            'allow_late_enrollment',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'option': {'write_only': True} # Option set contextually in the view
        }

    def validate(self, data):
        # Keep validation logic from original serializer
        # Need option context for validation
        option = data.get('option') or getattr(self.instance, 'option', None)
        if not option:
             raise serializers.ValidationError("Option context is required for schedule validation.")

        booking_type = option.booking_type
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        date = data.get('date') # Single session date
        day = data.get('day') # Course day

        if booking_type == 'Full Course':
            if not all([start_date, end_date, day]):
                raise serializers.ValidationError("Start date, end date, and day required for courses.")
            if start_date >= end_date:
                raise serializers.ValidationError("Course end date must be after start date.")
        else: # Single Session
            if not date:
                raise serializers.ValidationError("Date is required for single sessions.")
            # Automatically set day from date for single sessions if not provided
            if date and not data.get('day'):
                 data['day'] = date.strftime('%a')


        # Add other validations (duration, participants, price)
        if data.get('duration', 60) < 15:
            raise serializers.ValidationError({'duration': 'Duration must be at least 15 minutes.'})
        if data.get('maxParticipants', 1) < 1:
            raise serializers.ValidationError({'maxParticipants': 'Max participants must be at least 1.'})
        if data.get('price', 0) <= 0:
             raise serializers.ValidationError({'price': 'Price must be positive.'})


        return data

    # create/update logic often handled in view or model save

class ManagedClassOptionSerializer(serializers.ModelSerializer):
    """Serializer for managing class options by business users."""
    schedules = ScheduleSerializer(many=True, read_only=True) # Show schedules for management
    image_url = serializers.SerializerMethodField()
    # Add annotation counts if needed
    total_students = serializers.IntegerField(read_only=True)
    active_schedules_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ClassOption
        # Include all fields needed for management, including 'active'
        fields = [
            'optionId', 'classId', 'title', 'description',
            'booking_type', 'level', 'equipment', 'tags',
            'cancellationPolicy', 'price_type', # Allow managing price type
            'image', 'image_url', 
            'createdAt', 'updatedAt', 'schedules',
            'total_students', 'active_schedules_count' # Example computed fields
        ]
        read_only_fields = ['optionId', 'classId', 'createdAt', 'updatedAt', 'image_url', 'schedules', 'total_students', 'active_schedules_count']
        extra_kwargs = {
            'image': {'write_only': True, 'required': False}, # Image is write-only here, URL is provided
        }

    def get_image_url(self, obj):
        if obj.image and hasattr(obj.image, 'url'):
             try:
                 return obj.image.url
             except ValueError: return None
        return None

class ManagedClassSerializer(serializers.ModelSerializer):
    """Serializer for business users managing their classes."""
    options = ManagedClassOptionSerializer(many=True, read_only=True) # Use managed option serializer
    images = ClassImageSerializer(many=True, read_only=True)
    business_name = serializers.CharField(source='businessId.businessName', read_only=True)
    category_key = serializers.CharField(source='category.key', read_only=True, allow_null=True)
    category_name = serializers.CharField(source='category.name', read_only=True, allow_null=True)
    subcategory_key = serializers.CharField(source='subcategory.key', read_only=True, allow_null=True)
    subcategory_name = serializers.CharField(source='subcategory.name', read_only=True, allow_null=True)
    # Add annotations if needed
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__' # Include all fields for management view
        read_only_fields = [
            'classId', 'businessId', # Set contextually
            'createdAt', 'updatedAt',
            'options', 'images', # Managed via separate actions/nested reads
            'business_name',
            'category_key', 'category_name', # Read-only representations
            'subcategory_key', 'subcategory_name',
            'average_rating', 'review_count' # Read-only computed fields
        ]

class ClassCreateSerializer(serializers.ModelSerializer):
    """Serializer specifically for creating new classes."""
    # Use PrimaryKeyRelatedField for category/subcategory during creation for simplicity?
    # Or keep using keys and resolve in the view/serializer.create
    category_key = serializers.CharField(write_only=True, required=True)
    subcategory_key = serializers.CharField(write_only=True, required=False, allow_blank=True)

    # Handle images and options during creation in the view or serializer.create
    # images = serializers.ListField(child=serializers.ImageField(), write_only=True, required=False)
    # options = serializers.JSONField(write_only=True, required=True) # Expect JSON string for options

    class Meta:
        model = ClassesMain
        # List fields needed for creation
        fields = [
            'title', 'description', 'features',
            'category_key', 'subcategory_key', # Use keys for input
            'location', 'coordinates', 'saltLocation',
            'studentContactEmail', 'studentContactPhone',
            'adminContactEmail', 'adminContactPhone',
            # 'images', 'options' # Handled separately
        ]
        # Exclude fields set automatically (businessId, status, timestamps) or read-only representations

    def validate_category_key(self, value):
        if not ClassCategory.objects.filter(key=value).exists():
            raise serializers.ValidationError(f"Category with key '{value}' not found.")
        return value

    def validate(self, data):
        category_key = data.get('category_key')
        subcategory_key = data.get('subcategory_key')

        if subcategory_key and category_key:
            category = ClassCategory.objects.filter(key=category_key).first()
            if category and not ClassSubcategory.objects.filter(category=category, key=subcategory_key).exists():
                raise serializers.ValidationError({
                    'subcategory_key': f"Subcategory '{subcategory_key}' not found in category '{category_key}'."
                })
        # Add other cross-field validations if needed
        return data

    # Note: The actual creation logic involving images, options JSON parsing,
    # and associating with the business happens in the BusinessClassViewSet.perform_create method.