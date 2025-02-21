from datetime import date, datetime
import json
from random import uniform
from rest_framework import serializers
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q
from ..utils.permissions import check_user_role
from ..models import BusinessInfo, ClassesMain, ClassImage, ClassOption, Schedule, ScheduleInstance, ScheduleBreak, Booking
from django.db import transaction
from .auth_serializers import CustomUserDetailsSerializer


class ClassOptionCreateSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=False)
    equipment = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    
    class Meta:
        model = ClassOption
        fields = [
            'title', 
            'description', 
            'booking_type',
            'level',
            'equipment', 
            'tags', 
            'cancellationPolicy',
            'image', 
            'active'
        ]

    def validate(self, data):
        # Simplified validation since we removed scheduling fields
        if not data.get('booking_type') in ['Single Session', 'Full Course']:
            raise serializers.ValidationError({
                'booking_type': 'Invalid booking type'
            })

        return data

    def create(self, validated_data):
        equipment = validated_data.pop('equipment', [])
        tags = validated_data.pop('tags', [])
        
        option = ClassOption.objects.create(
            **validated_data,
            equipment=equipment,
            tags=tags
        )
        
        return option

    def update(self, instance, validated_data):
        if 'equipment' in validated_data:
            instance.equipment = validated_data.pop('equipment')
        if 'tags' in validated_data:
            instance.tags = validated_data.pop('tags')

        if 'image' in validated_data:
            if instance.image:
                instance.image.delete()
            instance.image = validated_data.pop('image')
            
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance

    def to_representation(self, instance):
        """
        Override to customize the serialized output
        """
        data = super().to_representation(instance)
        
        # Add any computed or custom fields
        data['image_url'] = instance.get_image_url() if instance.image else None
        
        # Include schedule information if available
        schedules = Schedule.objects.filter(option=instance)
        if schedules.exists():
            data['schedules'] = ScheduleSerializer(schedules, many=True).data
            
        return data

class ClassCreateSerializer(serializers.ModelSerializer):
    images = serializers.ListField(
        child=serializers.ImageField(),
        write_only=True,
        required=False
    )
    options = serializers.CharField(write_only=True)

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
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            raise serializers.ValidationError("User must be authenticated to create a class")

        try:
            options = json.loads(data.get('options', '[]'))
            if not options:
                raise serializers.ValidationError({
                    'options': ['At least one class option is required']
                })
                
            # Validate each option has required fields
            for option in options:
                if not all(key in option for key in ['title', 'description', 'booking_type']):
                    raise serializers.ValidationError({
                        'options': ['Each option must have title, description, and booking type']
                    })
        except json.JSONDecodeError:
            raise serializers.ValidationError("Invalid options format")

        return data

    def create(self, validated_data):
        images = self.context['request'].FILES.getlist('class_images')
        options_data = json.loads(validated_data.pop('options'))
        
        try:
            with transaction.atomic():
                user = self.context['request'].user
                business = BusinessInfo.objects.get(
                    Q(owner=user) | Q(managers=user)
                )
                
                class_instance = ClassesMain.objects.create(
                    businessId=business,
                    **validated_data
                )

                for image in images:
                    ClassImage.objects.create(
                        classId=class_instance,
                        image=image
                    )

                for index, option_data in enumerate(options_data):
                    option_image = self.context['request'].FILES.get(f'option_{index}_image')
                    
                    ClassOption.objects.create(
                        classId=class_instance,
                        image=option_image,
                        **option_data
                    )

                return class_instance

        except Exception as e:
            raise serializers.ValidationError(str(e))
        
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
            'instructor_notes',
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
    allow_late_enrollment = serializers.BooleanField(required=False, default=False)
    duration = serializers.IntegerField(required=False, default=60)
    
    class Meta:
        model = Schedule
        fields = [
            'id', 'option', 'day', 'time', 'duration',
            'price', 'maxParticipants', 'is_active',
            'start_date', 'end_date', 'date',
            'allow_late_enrollment'
        ]

    def validate(self, data):
        # Get the option and its booking type
        option = data.get('option')
        if not option:
            if self.instance:
                option = self.instance.option
            if not option:
                raise serializers.ValidationError({
                    'option': 'Option is required'
                })

        # Validate based on booking type
        if option.booking_type == 'Full Course':
            if not all([data.get('start_date'), data.get('end_date'), data.get('day')]):
                raise serializers.ValidationError({
                    'course_dates': 'Start date, end date, and day required for courses'
                })
            if data['start_date'] >= data['end_date']:
                raise serializers.ValidationError({
                    'course_dates': 'End date must be after start date'
                })
            if data['start_date'] < timezone.now().date():
                raise serializers.ValidationError({
                    'start_date': 'Course cannot start in the past'
                })
        else:
            if not data.get('date'):
                raise serializers.ValidationError({
                    'date': 'Date is required for single sessions'
                })
            if data['date'] < timezone.now().date():
                raise serializers.ValidationError({
                    'date': 'Session cannot be scheduled in the past'
                })

        # Common validations
        if data.get('duration', 60) < 15 or data.get('duration', 60) > 480:
            raise serializers.ValidationError({
                'duration': 'Duration must be between 15 and 480 minutes'
            })
            
        if data.get('maxParticipants', 0) < 1:
            raise serializers.ValidationError({
                'maxParticipants': 'Maximum participants must be at least 1'
            })
            
        if data.get('price', 0) <= 0:
            raise serializers.ValidationError({
                'price': 'Price must be greater than 0'
            })
            
        return data


    def create(self, validated_data):
        with transaction.atomic():
            # Create the schedule
            schedule = super().create(validated_data)
            
            # Schedule's save method will handle instance creation
            # based on booking type (single session vs course)
            
            return schedule

    def update(self, instance, validated_data):
        with transaction.atomic():
            # Store old values for comparison
            old_day = instance.day
            old_time = instance.time
            old_start_date = instance.start_date
            old_end_date = instance.end_date
            old_date = instance.date
            
            # Update the schedule
            schedule = super().update(instance, validated_data)
            
            # For courses, check if we need to regenerate instances
            if schedule.option.booking_type == 'Full Course':
                day_changed = validated_data.get('day') != old_day
                time_changed = validated_data.get('time') != old_time
                dates_changed = (
                    validated_data.get('start_date') != old_start_date or
                    validated_data.get('end_date') != old_end_date
                )
                
                # Regenerate instances if schedule changed
                if day_changed or time_changed or dates_changed:
                    # Delete future instances
                    instance.instances.filter(
                        date__gte=timezone.now().date()
                    ).delete()
                    
                    # Regenerate course instances
                    schedule.generate_course_instances()
            else:
                # For single sessions, update the instance if it exists
                date_changed = validated_data.get('date') != old_date
                time_changed = validated_data.get('time') != old_time
                
                if date_changed or time_changed:
                    instance.instances.all().delete()
                    # The save method will create a new instance
                    schedule.save()
            
            return schedule
    
class ClassOptionSerializer(serializers.ModelSerializer):
    schedules = serializers.SerializerMethodField()
    booking_type = serializers.CharField(source='get_booking_type_display')
    image_url = serializers.SerializerMethodField()
    total_students = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = ClassOption
        fields = [
            'optionId', 'title', 'description', 
            'booking_type', 'level',
            'equipment', 'tags',
            'cancellationPolicy', 'active', 'schedules',
            'image', 'image_url', 'total_students'
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

        # Validate price type based on booking type
        price_type = data.get('price_type')
        valid_price_types = {
            'single': ['per_session'],
            'course': ['per_session', 'full_course'],
        }
        
        if price_type not in valid_price_types.get(booking_type, []):
            raise serializers.ValidationError({
                'price_type': f'Invalid price type for {booking_type} booking'
            })

        return data

class ClassImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(required=True)  # Direct image field
    
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

    def create(self, validated_data):
        return ClassImage.objects.create(**validated_data)

class ClassesMainSerializer(serializers.ModelSerializer):
    options = ClassOptionSerializer(many=True, read_only=True)  # Add this back
    images = ClassImageSerializer(many=True, read_only=True)
    business_name = serializers.CharField(read_only=True)
    business_image = serializers.SerializerMethodField()
    coordinates = serializers.SerializerMethodField()
    average_rating = serializers.FloatField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ClassesMain
        fields = [
            'classId', 'businessId', 'title', 'description',
            'features', 'category', 'subcategory', 'coordinates',
            'saltLocation', 'studentContactEmail', 'studentContactPhone',
            'createdAt', 'updatedAt', 'images', 'business_name',
            'business_image', 'options', 'average_rating',
            'review_count'
        ]

    def get_business_image(self, obj):
        if obj.businessId and obj.businessId.businessImage:
            return obj.businessId.businessImage.url
        return None

    def get_coordinates(self, obj):
        if not obj.coordinates:
            return None
            
        try:
            lat, lng = map(float, obj.coordinates.split(','))
            
            if obj.saltLocation:
                # Salt coordinates by a small random amount (approximately 100-200 meters)
                # 0.001 degree is approximately 111 meters
                lat_salt = uniform(-0.0005, 0.0005)
                lng_salt = uniform(-0.0005, 0.0005)
                lat += lat_salt
                lng += lng_salt
                
            return f"{lat:.8f},{lng:.8f}"
        except:
            return None