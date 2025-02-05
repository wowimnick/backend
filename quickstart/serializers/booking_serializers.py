from rest_framework import serializers
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import transaction

from quickstart.constants import BookingChoices

from ..models import Booking, Schedule, ScheduleInstance

class BookingCreateSerializer(serializers.Serializer):
    schedule_instances = serializers.ListField(
        child=serializers.IntegerField(),
        min_length=1
    )

    def validate_schedule_instances(self, value):
        instances = ScheduleInstance.objects.select_related(
            'schedule__option'
        ).filter(id__in=value)
        
        if len(instances) != len(value):
            raise serializers.ValidationError('One or more schedule instances not found')

        first_instance = instances[0]
        booking_type = first_instance.schedule.option.booking_type

        # Only validate number of slots for recurring bookings
        if (booking_type == 'Recurring Classes' and 
            len(instances) != first_instance.schedule.option.sessions_per_week):
            raise serializers.ValidationError(
                f'Must select {first_instance.schedule.option.sessions_per_week} slots per week'
            )

        # Check current instances' availability
        for instance in instances:
            if instance.status != 'scheduled':
                raise serializers.ValidationError(
                    f'Instance {instance.id} is not available for booking'
                )
            
            if instance.date < timezone.now().date():
                raise serializers.ValidationError(
                    f'Cannot book past instance {instance.id}'
                )
            
            if not instance.can_accommodate(1):
                raise serializers.ValidationError(
                    f'Not enough spots available for instance {instance.id}'
                )
        
        return value

    def _check_future_availability(self, initial_instances, recurrence_pattern):
        """
        Check availability for future recurring instances
        Returns list of unavailable slots if any
        """
        weeks_to_check = 4  # One month
        if recurrence_pattern == 'biweekly':
            weeks_to_check = 8  # Two months to get 4 sessions

        unavailable_slots = []
        
        for instance in initial_instances:
            current_date = instance.date
            schedule = instance.schedule
            
            # Calculate future dates based on pattern
            future_dates = []
            for week in range(1, weeks_to_check + 1):
                if recurrence_pattern == 'biweekly' and week % 2 == 1:
                    continue
                future_date = current_date + timedelta(weeks=week)
                future_dates.append(future_date)

            # Check each future date
            for future_date in future_dates:
                future_instance = ScheduleInstance.objects.filter(
                    schedule=schedule,
                    date=future_date,
                    time=instance.time
                ).first()

                if not future_instance or not future_instance.can_accommodate(1):
                    unavailable_slots.append({
                        'original_instance_id': instance.id,
                        'date': future_date,
                        'time': instance.time,
                        'reason': 'No availability'
                    })

        return unavailable_slots

    def validate(self, data):
        first_instance = ScheduleInstance.objects.select_related(
            'schedule__option'
        ).get(id=data['schedule_instances'][0])
        
        option = first_instance.schedule.option
        booking_type = option.booking_type  
        
        # For single bookings, don't validate sessions_per_week
        if booking_type == 'Single Session':
            data['sessions_per_week'] = 1
            data['recurrence_pattern'] = None
        else:
            data['sessions_per_week'] = option.sessions_per_week
            data['recurrence_pattern'] = option.recurrence_pattern
            
        data['booking_type'] = booking_type
        
        return data

class RelatedBookingSerializer(serializers.ModelSerializer):
    """Simplified serializer for related bookings to avoid recursion"""
    date = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    
    class Meta:
        model = Booking
        fields = [
            'id', 'date', 'time', 'status', 'participants',
            'amount_paid', 'attendance_marked', 'attended'
        ]
    
    def get_date(self, obj):
        return obj.schedule_instance.date if obj.schedule_instance else None

    def get_time(self, obj):
        return obj.schedule_instance.time if obj.schedule_instance else None

class BookingDetailSerializer(serializers.ModelSerializer):
    class_name = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    student_name = serializers.SerializerMethodField()
    student_email = serializers.SerializerMethodField()
    enrollment_details = serializers.SerializerMethodField()
    group_bookings = serializers.SerializerMethodField()
    
    class Meta:
        model = Booking
        fields = [
            'id', 'booking_group_id', 'schedule_instance', 'date', 'time',
            'student_name', 'student_email', 'class_name', 'business_name',
            'participants', 'notes', 'status', 'booking_date', 'cancelled_at',
            'cancellation_reason', 'amount_paid', 'payment_status',
            'enrollment_type', 'enrollment_details', 'attendance_marked', 
            'attended', 'group_bookings'
        ]
    
    def get_date(self, obj):
        return obj.schedule_instance.date if obj.schedule_instance else None

    def get_time(self, obj):
        return obj.schedule_instance.time if obj.schedule_instance else None

    def get_student_name(self, obj):
        if obj.student and obj.student.user:
            return f"{obj.student.user.first_name} {obj.student.user.last_name}".strip()
        return None

    def get_student_email(self, obj):
        return obj.student.user.email if obj.student and obj.student.user else None

    def get_class_name(self, obj):
        if obj.schedule_instance and obj.schedule_instance.schedule:
            return obj.schedule_instance.schedule.option.classId.title
        return None

    def get_business_name(self, obj):
        if obj.schedule_instance and obj.schedule_instance.schedule:
            return obj.schedule_instance.schedule.option.classId.businessId.businessName
        return None

    def get_enrollment_details(self, obj):
        if not obj.booking_group_id:
            return None
            
        details = {
            'type': obj.enrollment_type,
            'sessions': [],
        }
        
        # Get all related bookings for the same group
        related_bookings = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).order_by('schedule_instance__date', 'schedule_instance__time')
        
        for booking in related_bookings:
            details['sessions'].append({
                'date': booking.schedule_instance.date,
                'time': booking.schedule_instance.time,
                'status': booking.status
            })
            
        return details

    def get_group_bookings(self, obj):
        """Get related bookings using the simplified serializer"""
        if not obj.booking_group_id:
            return []
            
        related_bookings = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).exclude(id=obj.id)
        
        return RelatedBookingSerializer(related_bookings, many=True).data