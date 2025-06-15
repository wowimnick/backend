# serializers/classes/business_class_serializers.py
from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from django.db.models.functions import Coalesce
from django.db.models import Q, Sum
from django.utils import timezone
import json
import logging

# Adjust import paths as needed
from ...models import (
    Booking, BusinessInfo, ClassCategory, ClassSubcategory, ClassesMain, ClassImage,
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
            'current_bookings_count', 'available_spots',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'schedule', 'created_at', 'updated_at', 'current_bookings_count', 'available_spots']

    def get_available_spots(self, obj):
        # Calculation based on annotation or property
        current_bookings = getattr(obj, 'current_bookings_count', 0)
        return obj.max_participants - current_bookings
class ScheduleSerializer(serializers.ModelSerializer):
    """Serializer for creating/managing schedules within a class option."""
    booked_participants = serializers.SerializerMethodField()
    total_revenue = serializers.SerializerMethodField()
    has_confirmed_bookings = serializers.SerializerMethodField() # For edit/delete disabling

    class Meta:
        model = Schedule
        fields = [
            'id', 'option', 'day', 'time', 'duration',
            'price', 'maxParticipants',
            'start_date', 'end_date', 
            'date', 
            'allow_late_enrollment',
            'booked_participants', # Added
            'total_revenue',       # Added
            'has_confirmed_bookings', # Added
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'booked_participants', 'total_revenue', 'has_confirmed_bookings']
        extra_kwargs = {
            'option': {'write_only': True} 
        }

    def get_booked_participants(self, obj):
        # Sum of participants from confirmed bookings for all instances of this schedule
        # This sums across all instances of a recurring schedule.
        # If you need it per-instance, that's better done on ScheduleInstanceSerializer
        return Booking.objects.filter(
            schedule_instance__schedule=obj,
            status='confirmed'
        ).aggregate(total_booked=Coalesce(Sum('participants'), 0))['total_booked']

    def get_total_revenue(self, obj):
        # Sum of amount_paid from confirmed & paid bookings for all instances of this schedule
        return Booking.objects.filter(
            schedule_instance__schedule=obj,
            status='confirmed',
            payment_status='paid'
        ).aggregate(total_revenue=Coalesce(Sum('amount_paid'), Decimal('0.00')))['total_revenue']

    def get_has_confirmed_bookings(self, obj):
        # Checks if any instance of this schedule (past or future) has a confirmed booking.
        # If a schedule instance is in the past but had a booking, we still might not want to delete the parent Schedule.
        # However, for EDITING, we primarily care about future instances with bookings.
        # For DELETE, we care about any instance with bookings.
        # This flag will make it simple on the frontend to disable edit/delete if ANY confirmed booking exists for the schedule.
        return Booking.objects.filter(
            schedule_instance__schedule=obj,
            status='confirmed'
        ).exists()


    def validate(self, data):
        option = data.get('option') or getattr(self.instance, 'option', None)
        if not option:
             raise serializers.ValidationError("Option context is required for schedule validation.")

        booking_type = option.booking_type
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        date_field = data.get('date') 
        day = data.get('day')

        # Prevent editing critical fields if there are confirmed bookings for any instance of this schedule
        if self.instance and self.instance.pk: # If updating an existing schedule
            # Fields that, if changed, would fundamentally alter the schedule for existing bookers
            critical_fields_being_changed = any(
                data.get(field) is not None and data.get(field) != getattr(self.instance, field)
                for field in ['day', 'time', 'duration', 'price', 'start_date', 'end_date', 'date']
            )
            if critical_fields_being_changed:
                 if Booking.objects.filter(schedule_instance__schedule=self.instance, status='confirmed').exists():
                      raise serializers.ValidationError(
                          "This schedule has confirmed bookings and critical details (like date, time, price) cannot be changed. "
                          "Please cancel the existing schedule and create a new one if significant changes are needed."
                      )
        
        if booking_type == 'Full Course':
            if not all([start_date, end_date, day]):
                raise serializers.ValidationError("Start date, end date, and day required for courses.")
            if start_date >= end_date:
                raise serializers.ValidationError("Course end date must be after start date.")
            if (self.instance is None or self.instance.pk is None) and start_date < timezone.now().date():
              raise serializers.ValidationError({'start_date': 'New course cannot start in the past.'})

        else: 
            if not date_field:
                raise serializers.ValidationError("Date is required for single sessions.")
            if date_field and not data.get('day'): # If day wasn't sent, derive it
                 data['day'] = date_field.strftime('%a')
            if (self.instance is None or self.instance.pk is None) and date_field < timezone.now().date():
              raise serializers.ValidationError({'date': 'New session cannot be scheduled in the past.'})


        if data.get('duration', 60) < 15:
            raise serializers.ValidationError({'duration': 'Duration must be at least 15 minutes.'})
        
        # Allow maxParticipants to be updated even if there are bookings,
        # but it cannot be set lower than current confirmed bookings.
        new_max_participants = data.get('maxParticipants')
        if new_max_participants is not None:
            if new_max_participants < 1:
                raise serializers.ValidationError({'maxParticipants': 'Max participants must be at least 1.'})
            if self.instance and self.instance.pk:
                current_booked_sum = Booking.objects.filter(
                    schedule_instance__schedule=self.instance,
                    status='confirmed'
                ).aggregate(total_booked=Coalesce(Sum('participants'), 0))['total_booked']
                if new_max_participants < current_booked_sum:
                    raise serializers.ValidationError({
                        'maxParticipants': f'Cannot set capacity below current confirmed bookings ({current_booked_sum}).'
                    })
        
        if data.get('price', 0) < 0: 
             raise serializers.ValidationError({'price': 'Price cannot be negative.'})

        return data

class ManagedClassOptionSerializer(serializers.ModelSerializer):
    """Serializer for managing class options by business users."""
    schedules = ScheduleSerializer(many=True, read_only=True)
    total_students = serializers.IntegerField(read_only=True) # Assuming this is annotated in the queryset
    active_schedules_count = serializers.IntegerField(read_only=True) # Assuming this is annotated

    class Meta:
        model = ClassOption
        fields = [
            'optionId', 'classId', 
            'booking_type', 'level', 'equipment', 'tags',
            'cancellationPolicy', 'cancellationRefundPercentage',
            'price_type',
            'createdAt', 'updatedAt', 'schedules',
            'total_students', 'active_schedules_count'
        ]
        read_only_fields = [
            'optionId', 'classId', 'createdAt', 'updatedAt', 
            'schedules',
            'total_students', 'active_schedules_count'
        ]
        extra_kwargs = {
            'equipment': {'required': False},
            'tags': {'required': False},
            'level': {'default': ClassOption._meta.get_field('level').get_default()},
            'cancellationPolicy': {'default': ClassOption._meta.get_field('cancellationPolicy').get_default()},
            'cancellationRefundPercentage': {'default': ClassOption._meta.get_field('cancellationRefundPercentage').get_default()},
            'booking_type': {'default': ClassOption._meta.get_field('booking_type').get_default()},
            'price_type': {'default': ClassOption._meta.get_field('price_type').get_default()},
        }

    def get_image_url(self, obj): 
        if obj.image and hasattr(obj.image, 'url'):
             try:
                 return obj.image.url
             except ValueError:
                 return None
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