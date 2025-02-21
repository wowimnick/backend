import uuid
from rest_framework import serializers
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import transaction

from ..models import (
    Booking,
    ClassImage, 
    Schedule, 
    ScheduleInstance,
    CustomUser
)

import logging
logger = logging.getLogger(__name__)

class BookingCreateSerializer(serializers.Serializer):
    selectedSlots = serializers.ListField(
        child=serializers.DictField(),
        min_length=1
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    participants = serializers.IntegerField(default=1, min_value=1, max_value=4)

    def validate_selectedSlots(self, value):
        if not value:
            raise serializers.ValidationError('At least one slot must be selected')
            
        # Get the first slot to determine the schedule instance
        first_slot = value[0]
        slot_id = first_slot.get('id')
        
        if not slot_id:
            raise serializers.ValidationError('Slot ID is required')
            
        try:
            instance = ScheduleInstance.objects.select_related(
                'schedule',
                'schedule__option'
            ).get(id=slot_id)
            
            if instance.status != 'scheduled':
                raise serializers.ValidationError('This session is not available for booking')
            
            if instance.date < timezone.now().date():
                raise serializers.ValidationError('Cannot book past sessions')
            
            participants = getattr(self.context.get('request'), 'data', {}).get('participants', 1)
            if not instance.can_accommodate(participants):
                raise serializers.ValidationError('Not enough spots available')
            
            # Store the instance for later use in validate()
            self.instance = instance
            return value
            
        except ScheduleInstance.DoesNotExist:
            raise serializers.ValidationError('Invalid schedule instance')

    def validate(self, data):
        # Use the instance we stored in validate_selectedSlots
        instance = getattr(self, 'instance', None)
        if not instance:
            raise serializers.ValidationError('Schedule instance validation failed')

        # For course bookings, validate all future instances have space
        if instance.schedule.option.booking_type == 'Full Course':
            future_instances = ScheduleInstance.objects.filter(
                schedule=instance.schedule,
                date__gte=instance.date,
                date__lte=instance.schedule.end_date,
                status='scheduled'
            )
            
            for future_instance in future_instances:
                if not future_instance.can_accommodate(data['participants']):
                    raise serializers.ValidationError({
                        'participants': f'Not enough spots available for session on {future_instance.date}'
                    })
        else:
            # Single session validation
            if not instance.can_accommodate(data['participants']):
                raise serializers.ValidationError({
                    'participants': 'Not enough spots available for requested number of participants'
                })
            
        return data

    def create(self, validated_data):
        user = self.context['request'].user
        first_slot = validated_data['selectedSlots'][0]
        initial_instance = self.instance  # Use the stored instance
        
        booking_group_id = uuid.uuid4() if initial_instance.schedule.option.booking_type == 'Full Course' else None
        
        try:
            with transaction.atomic():
                if initial_instance.schedule.option.booking_type == 'Full Course':
                    # Get all instances for the course
                    course_instances = ScheduleInstance.objects.filter(
                        schedule=initial_instance.schedule,
                        date__gte=initial_instance.date,
                        date__lte=initial_instance.schedule.end_date,
                        status='scheduled'
                    ).order_by('date')
                    
                    bookings = []
                    for instance in course_instances:
                        booking = Booking.objects.create(
                            schedule_instance=instance,
                            user=user,
                            booking_group_id=booking_group_id,
                            participants=validated_data['participants'],
                            notes=validated_data.get('notes', ''),
                            amount_paid=instance.price * validated_data['participants'],
                            status='confirmed',
                            payment_status='paid',
                            enrollment_type='Full Course'
                        )
                        bookings.append(booking)
                    
                    return bookings[0]
                else:
                    # Single session booking
                    return Booking.objects.create(
                        schedule_instance=initial_instance,
                        user=user,
                        participants=validated_data['participants'],
                        notes=validated_data.get('notes', ''),
                        amount_paid=initial_instance.price * validated_data['participants'],
                        status='confirmed',
                        payment_status='paid',
                        enrollment_type='Single Session'
                    )
                    
        except Exception as e:
            logger.error(f"Error creating booking: {str(e)}")
            raise serializers.ValidationError("Failed to create booking")

# Booking list serializer for business bookings
class BookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration')
    booking_type = serializers.CharField(source='enrollment_type')
    session_info = serializers.SerializerMethodField()

    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course':
            # Get all bookings in the same group
            related_bookings = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date')
            
            total_sessions = related_bookings.count()
            current_session = list(related_bookings).index(obj) + 1
            
            return {
                'current_session': current_session,
                'total_sessions': total_sessions
            }
        return None

    class Meta:
        model = Booking
        fields = [
            'id', 'user_name', 'user_email', 'class_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'status', 'enrollment_type',
            'booking_type', 'amount_paid', 'payment_status',
            'notes', 'booking_date', 'attendance_marked',
            'attended', 'session_info'
        ]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()
    

# Student booking serializer for student bookings
class StudentBookingSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    coordinates = serializers.CharField(source='schedule_instance.schedule.option.classId.coordinates')
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName')
    price = serializers.DecimalField(source='amount_paid', max_digits=10, decimal_places=2)
    class_image = serializers.SerializerMethodField()
    booking_id = serializers.IntegerField(source='id')
    has_review = serializers.SerializerMethodField()
    enrollment_type = serializers.CharField()
    session_info = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'booking_id',
            'class_name', 'option_name', 'date', 'time',
            'coordinates', 'business_name', 'price', 'status',
            'class_image', 'has_review', 'session_info', 'enrollment_type'
        ]

    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course':
            # Get all bookings in the same group
            related_bookings = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date')
            
            total_sessions = related_bookings.count()
            current_session = list(related_bookings).index(obj) + 1
            
            return {
                'current_session': current_session,
                'total_sessions': total_sessions
            }
        return None

    def get_class_image(self, obj):
        first_image = ClassImage.objects.filter(
            classId=obj.schedule_instance.schedule.option.classId
        ).first()
        if first_image:
            return first_image.image.url
        return None

    def get_has_review(self, obj):
        return hasattr(obj, 'review')

# Booking detail serializer for booking details, has more information
class BookingDetailSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration')
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email')
    
    class Meta:
        model = Booking
        fields = [
            'id', 'date', 'time', 'duration',
            'user_name', 'user_email', 'class_name', 
            'business_name', 'participants', 'notes', 
            'status', 'booking_date', 'cancelled_at',
            'cancellation_reason', 'amount_paid', 
            'payment_status', 'enrollment_type',
            'attendance_marked', 'attended', 
            'booking_group_id'
        ]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_course_details(self, obj):
        """Return course details if this is a course booking"""
        if obj.enrollment_type != 'Full Course':
            return None
            
        schedule = obj.schedule_instance.schedule
        return {
            'start_date': schedule.start_date,
            'end_date': schedule.end_date,
            'day': schedule.day,
            'time': schedule.time,
            'total_sessions': ScheduleInstance.objects.filter(
                schedule=schedule,
                date__range=[schedule.start_date, schedule.end_date]
            ).count()
        }

    def get_related_bookings(self, obj):
        """Get all bookings in the same course group"""
        if not obj.booking_group_id:
            return None
            
        related = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).exclude(id=obj.id).order_by('schedule_instance__date')
        
        return [{
            'id': booking.id,
            'date': booking.schedule_instance.date,
            'time': booking.schedule_instance.time,
            'status': booking.status,
            'attended': booking.attended,
            'attendance_marked': booking.attendance_marked
        } for booking in related]