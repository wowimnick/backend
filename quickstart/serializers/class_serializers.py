from datetime import date, datetime
import json
from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q
from ..views.permissions import check_user_role
from ..models import BusinessInfo, ClassesMain, ClassImage, ClassOption, Reviews, Schedule, ScheduleInstance, ScheduleBreak, Booking
from django.db import transaction
from .auth_serializers import CustomUserDetailsSerializer

class ClassOptionCreateSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=False)  # Add this line
    equipment = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    
    class Meta:
        model = ClassOption
        fields = [
            'title', 'description', 'booking_type',
            'duration', 'maxParticipants', 'level',
            'price', 'price_type', 'total_sessions',
            'start_date', 'end_date', 'recurrence_pattern',
            'sessions_per_week', 'auto_renew_default',
            'equipment', 'tags', 'cancellationPolicy',
            'image', 'active'  # Add image to fields
        ]

    def validate(self, data):
        booking_type = data.get('booking_type')
        price_type = data.get('price_type')
        
        # Validate booking type specific fields
        if booking_type == 'course':
            if not data.get('total_sessions'):
                raise serializers.ValidationError({
                    'total_sessions': 'Required for course booking type'
                })
            if not (data.get('start_date') and data.get('end_date')):
                raise serializers.ValidationError({
                    'course_dates': 'Start and end dates required for courses'
                })
            if data.get('start_date') and data.get('end_date'):
                if data['start_date'] >= data['end_date']:
                    raise serializers.ValidationError({
                        'course_dates': 'End date must be after start date'
                    })
                if data['start_date'] < date.today():
                    raise serializers.ValidationError({
                        'start_date': 'Start date cannot be in the past'
                    })
                    
        elif booking_type == 'recurring':
            if not data.get('recurrence_pattern'):
                raise serializers.ValidationError({
                    'recurrence_pattern': 'Required for recurring booking type'
                })
            if not data.get('sessions_per_week'):
                raise serializers.ValidationError({
                    'sessions_per_week': 'Required for recurring booking type'
                })
            if data.get('sessions_per_week', 0) > 7:
                raise serializers.ValidationError({
                    'sessions_per_week': 'Cannot exceed 7 sessions per week'
                })
                
        # Validate price type based on booking type
        valid_price_types = {
            'single': ['per_session'],
            'course': ['per_session', 'full_course'],
            'recurring': ['per_session', 'per_month']
        }
        
        if price_type not in valid_price_types.get(booking_type, []):
            raise serializers.ValidationError({
                'price_type': f'Invalid price type for {booking_type} booking'
            })
            
        # Validate duration
        if data.get('duration', 0) < 15 or data.get('duration', 0) > 480:
            raise serializers.ValidationError({
                'duration': 'Duration must be between 15 and 480 minutes'
            })
            
        # Validate maxParticipants
        if data.get('maxParticipants', 0) < 1:
            raise serializers.ValidationError({
                'maxParticipants': 'Maximum participants must be at least 1'
            })

        return data

    def create(self, validated_data):
        # Pop fields that need special handling
        equipment = validated_data.pop('equipment', [])
        tags = validated_data.pop('tags', [])
        
        # Clean up fields based on booking type
        if validated_data['booking_type'] != 'course':
            validated_data.pop('total_sessions', None)
            validated_data.pop('start_date', None)
            validated_data.pop('end_date', None)
            
        if validated_data['booking_type'] != 'recurring':
            validated_data.pop('recurrence_pattern', None)
            validated_data.pop('sessions_per_week', None)
            validated_data.pop('auto_renew_default', None)
        
        # Create the option with all data including the image
        option = ClassOption.objects.create(
            **validated_data,
            equipment=equipment,
            tags=tags
        )
        
        return option

    def update(self, instance, validated_data):
        # Handle equipment and tags
        if 'equipment' in validated_data:
            instance.equipment = validated_data.pop('equipment')
        if 'tags' in validated_data:
            instance.tags = validated_data.pop('tags')

        # Handle single image
        if 'image' in validated_data:
            # If there's an existing image, delete it
            if instance.image:
                instance.image.delete()
            instance.image = validated_data.pop('image')

        # Clean up fields based on booking type
        booking_type = validated_data.get('booking_type', instance.booking_type)
        if booking_type != 'course':
            validated_data.pop('total_sessions', None)
            validated_data.pop('start_date', None)
            validated_data.pop('end_date', None)
            instance.total_sessions = None
            instance.start_date = None
            instance.end_date = None
            
        if booking_type != 'recurring':
            validated_data.pop('recurrence_pattern', None)
            validated_data.pop('sessions_per_week', None)
            validated_data.pop('auto_renew_default', None)
            instance.recurrence_pattern = None
            instance.sessions_per_week = None
            instance.auto_renew_default = False

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance

class ClassCreateSerializer(serializers.ModelSerializer):
    images = serializers.ListField(
        child=serializers.ImageField(),
        write_only=True,
        required=False
    )
    options = serializers.CharField(write_only=True)  # To accept JSON string

    class Meta:
        model = ClassesMain
        fields = [
            'title', 'description', 'features', 'category', 'subcategory',
            'location', 'coordinates', 'saltLocation',
            'studentContactEmail', 'studentContactPhone',
            'adminContactEmail', 'adminContactPhone',
            'images', 'options'
        ]

    def validate(self, data):
        # Ensure the user is authenticated
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            raise serializers.ValidationError("User must be authenticated to create a class")

        # Validate options and images as before
        try:
            options = json.loads(data.get('options', '[]'))
            if not options:
                raise serializers.ValidationError({
                    'options': ['At least one class option is required']
                })
        except json.JSONDecodeError:
            raise serializers.ValidationError("Invalid options format")

        return data

    def create(self, validated_data):
        # Pop images and options from validated data
        images = validated_data.pop('images', [])
        options_json = validated_data.pop('options', '[]')
        
        # Get the current user's business
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            raise serializers.ValidationError("User must be authenticated to create a class")

        try:
            # Try to get the business for the current user
            business = BusinessInfo.objects.get(
                Q(owner=request.user) | Q(managers=request.user)
            )
        except BusinessInfo.DoesNotExist:
            raise serializers.ValidationError({
                'businessId': ['No business found for the current user']
            })

        try:
            options_data = json.loads(options_json)
        except json.JSONDecodeError:
            raise serializers.ValidationError("Invalid options format")

        with transaction.atomic():
            # Create the class instance with the business
            class_instance = ClassesMain.objects.create(
                businessId=business,  # Set the business here
                **validated_data
            )
            
            # Create ClassImage instances
            for image in images:
                ClassImage.objects.create(
                    classId=class_instance,
                    image=image
                )
            
            # Create ClassOption instances
            for option_index, option_data in enumerate(options_data):
                # Get the image key from the option data
                image_key = option_data.pop('image_key', None)
                # Get the actual image file using the key
                option_image = self.context['request'].FILES.get(image_key) if image_key else None
                
                ClassOption.objects.create(
                    classId=class_instance,
                    title=option_data['title'],
                    description=option_data['description'],
                    maxParticipants=option_data['maxParticipants'],
                    level=option_data['level'],
                    price=option_data['price'],
                    booking_type=option_data['type'],  # Changed from type to booking_type
                    equipment=option_data.get('equipment', []),
                    tags=option_data.get('tags', []),
                    cancellationPolicy=option_data['cancellationPolicy'],
                    image=option_image
                )
            
            return class_instance
        
class ScheduleBreakSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleBreak
        fields = ['id', 'schedule', 'start_date', 'end_date', 'reason', 'created_at']
        read_only_fields = ['created_at']

    def validate(self, data):
        if data['start_date'] >= data['end_date']:
            raise serializers.ValidationError({
                'end_date': 'End date must be after start date'
            })
            
        if data['start_date'] < timezone.now().date():
            raise serializers.ValidationError({
                'start_date': 'Break cannot start in the past'
            })
            
        # Check for overlapping breaks
        overlapping = ScheduleBreak.objects.filter(
            schedule=data['schedule'],
            start_date__lte=data['end_date'],
            end_date__gte=data['start_date']
        )
        
        if self.instance:
            overlapping = overlapping.exclude(pk=self.instance.pk)
            
        if overlapping.exists():
            raise serializers.ValidationError(
                'This break period overlaps with an existing break'
            )
            
        return data

class ScheduleInstanceSerializer(serializers.ModelSerializer):
    current_bookings = serializers.IntegerField(read_only=True)
    available_spots = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = ScheduleInstance
        fields = [
            'id', 'schedule', 'date', 'time', 'price',
            'max_participants', 'status', 'cancellation_reason',
            'attendance_marked', 'instructor_notes',
            'current_bookings', 'available_spots',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, data):
        if 'status' in data and data['status'] == 'cancelled':
            if not data.get('cancellation_reason'):
                raise serializers.ValidationError({
                    'cancellation_reason': 'Required when cancelling an instance'
                })
                
        if 'date' in data and data['date'] < timezone.now().date():
            raise serializers.ValidationError({
                'date': 'Cannot create or modify past instances'
            })
            
        return data

class ScheduleSerializer(serializers.ModelSerializer):
    instances = ScheduleInstanceSerializer(many=True, read_only=True)
    breaks = ScheduleBreakSerializer(many=True, read_only=True)
    
    class Meta:
        model = Schedule
        fields = [
            'id', 'option', 'day', 'time', 'price',
            'maxParticipants', 'is_active', 'effective_price',
            'effective_max_participants', 'instances', 'breaks',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, data):
        if 'price' in data and data['price'] is not None:
            if data['price'] <= Decimal('0'):
                raise serializers.ValidationError({
                    'price': 'Price override must be greater than 0'
                })
                
        if 'maxParticipants' in data and data['maxParticipants'] is not None:
            if data['maxParticipants'] < 1:
                raise serializers.ValidationError({
                    'maxParticipants': 'Maximum participants must be at least 1'
                })
                
        return data

    def create(self, validated_data):
        with transaction.atomic():
            schedule = super().create(validated_data)
            
            # Generate initial instances
            if schedule.is_active:
                schedule.generate_instances(
                    start_date=timezone.now().date()
                )
                
            return schedule

    def update(self, instance, validated_data):
        with transaction.atomic():
            # Store old values for comparison
            old_price = instance.effective_price
            old_capacity = instance.effective_max_participants
            old_time = instance.time
            old_day = instance.day
            
            # Update the schedule
            schedule = super().update(instance, validated_data)
            
            # Check what changed
            price_changed = schedule.effective_price != old_price
            capacity_changed = schedule.effective_max_participants != old_capacity
            time_changed = 'time' in validated_data and validated_data['time'] != old_time
            day_changed = 'day' in validated_data and validated_data['day'] != old_day
            
            if any([price_changed, capacity_changed, time_changed, day_changed]):
                # Update all future instances
                future_instances = schedule.instances.filter(
                    date__gte=timezone.now().date(),
                    status='scheduled'
                )
                
                for instance in future_instances:
                    if price_changed:
                        instance.price = schedule.effective_price
                    if capacity_changed:
                        instance.max_participants = schedule.effective_max_participants
                    if time_changed:
                        instance.time = schedule.time
                    instance.save()
                    
                # If day changed, we need to regenerate future instances
                if day_changed:
                    # Delete future instances
                    future_instances.delete()
                    # Generate new instances
                    schedule.generate_instances(
                        start_date=timezone.now().date()
                    )
            
            return schedule
    
class ClassOptionSerializer(serializers.ModelSerializer):
    schedules = serializers.SerializerMethodField()
    booking_type = serializers.CharField(source='get_booking_type_display')
    image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = ClassOption
        fields = [
            'optionId', 'title', 'description', 
            'booking_type', 'duration', 'maxParticipants',
            'level', 'price', 'price_type',
            'total_sessions', 'start_date', 'end_date',
            'recurrence_pattern', 'sessions_per_week',
            'auto_renew_default', 'equipment', 'tags',
            'cancellationPolicy', 'active', 'schedules',
            'image', 'image_url'
        ]
        
    def get_schedules(self, obj):
        return ScheduleSerializer(obj.schedules.all(), many=True).data

    def get_image_url(self, obj):
        if obj.image:
            return obj.image.url
        return None

    def validate(self, data):
        booking_type = data.get('booking_type')
        
        # Validate based on booking type
        if booking_type == 'course':
            if not data.get('total_sessions'):
                raise serializers.ValidationError({
                    'total_sessions': 'Required for course booking type'
                })
            if not (data.get('start_date') and data.get('end_date')):
                raise serializers.ValidationError({
                    'course_dates': 'Start and end dates required for courses'
                })
            if data['start_date'] >= data['end_date']:
                raise serializers.ValidationError({
                    'course_dates': 'End date must be after start date'
                })
                
        elif booking_type == 'recurring':
            if not data.get('recurrence_pattern'):
                raise serializers.ValidationError({
                    'recurrence_pattern': 'Required for recurring booking type'
                })
            if not data.get('sessions_per_week'):
                raise serializers.ValidationError({
                    'sessions_per_week': 'Required for recurring booking type'
                })
                
        # Validate price type based on booking type
        price_type = data.get('price_type')
        valid_price_types = {
            'single': ['per_session'],
            'course': ['per_session', 'full_course'],
            'recurring': ['per_session', 'per_month']
        }
        
        if price_type not in valid_price_types.get(booking_type, []):
            raise serializers.ValidationError({
                'price_type': f'Invalid price type for {booking_type} booking'
            })

        return data

class ClassImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

class ReviewSerializer(serializers.ModelSerializer):
    userId = CustomUserDetailsSerializer(read_only=True)

    class Meta:
        model = Reviews
        fields = ['reviewId', 'userId', 'rating', 'comment', 'createdAt']

class ClassesMainSerializer(serializers.ModelSerializer):
    options = ClassOptionSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)
    images = ClassImageSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__'