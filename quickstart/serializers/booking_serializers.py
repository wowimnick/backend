import uuid
from rest_framework import serializers
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from ..models import Booking, Schedule
from datetime import datetime
from django.utils import timezone
from django.db import transaction

class BookingCreateSerializer(serializers.ModelSerializer):
    # Add fields for enrollment types
    enrollment_type = serializers.ChoiceField(
        choices=[
            ('Single Session', 'Single Session'),
            ('Full Course', 'Full Course'), 
            ('Recurring Classes', 'Recurring Classes')
        ], 
        required=True
    )
    course_start_date = serializers.DateField(required=False)
    course_end_date = serializers.DateField(required=False)
    total_sessions = serializers.IntegerField(required=False)
    sessions_per_week = serializers.IntegerField(required=False)
    recurrence_pattern = serializers.ChoiceField(choices=['weekly', 'biweekly'], required=False)
    booking_group_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'schedule_instance', 'participants', 'notes',
            'enrollment_type', 'course_start_date', 'course_end_date',
            'total_sessions', 'sessions_per_week', 'recurrence_pattern',
            'booking_group_id'
        ]
        
    def validate(self, data):
        print("Starting validation with data:", data)  # Add this
        instance = data['schedule_instance']
        participants = data['participants']
        enrollment_type = data.get('enrollment_type')  # Use get() to avoid KeyError
        
        print(f"Validating booking: type={enrollment_type}, participants={participants}, instance={instance}")  # Add this
        
        # Validate basic booking requirements
        if instance.status != 'scheduled':
            print(f"Instance status validation failed: {instance.status}")  # Add this
            raise serializers.ValidationError(
                'This class instance is not available for booking'
            )
            
        if instance.date < timezone.now().date():
            print(f"Date validation failed: {instance.date}")  # Add this
            raise serializers.ValidationError(
                'Cannot book past class instances'
            )
            
        if not instance.can_accommodate(participants):
            print(f"Capacity validation failed: available={instance.available_spots}, requested={participants}")  # Add this
            raise serializers.ValidationError(
                f'Only {instance.available_spots} spots remaining'
            )

        # Validate enrollment type specific fields
        print(f"Checking enrollment type specific fields for: {enrollment_type}")  # Add this
        if enrollment_type == 'Full Course':
            required_fields = [
                'course_start_date',
                'course_end_date',
                'total_sessions',
                'sessions_per_week',
                'recurrence_pattern'
            ]
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                print(f"Missing required fields for Full Course: {missing_fields}")  # Add this
                raise serializers.ValidationError(
                    'Course bookings require start_date, end_date, total_sessions, sessions_per_week, and recurrence_pattern'
                )
            if data['course_start_date'] >= data['course_end_date']:
                print("Course date validation failed")  # Add this
                raise serializers.ValidationError('End date must be after start date')
            if data['sessions_per_week'] < 1:
                print("Sessions per week validation failed")  # Add this
                raise serializers.ValidationError('Must have at least one session per week')

        elif enrollment_type == 'Recurring Classes':
            required_fields = ['sessions_per_week', 'recurrence_pattern']
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                print(f"Missing required fields for Recurring Classes: {missing_fields}")  # Add this
                raise serializers.ValidationError(
                    'Recurring bookings require sessions_per_week and recurrence_pattern'
                )
            if data['sessions_per_week'] < 1:
                print("Sessions per week validation failed")  # Add this
                raise serializers.ValidationError('Must have at least one session per week')
        
        print("Validation successful")  # Add this
        return data

    def create(self, validated_data):
        print("Starting booking creation with data:", validated_data)  # Add this
        with transaction.atomic():
            try:
                instance = validated_data['schedule_instance']
                participants = validated_data['participants']
                enrollment_type = validated_data['enrollment_type']
                
                print(f"Creating booking: type={enrollment_type}, participants={participants}")  # Add this
                
                if enrollment_type == 'Full Course':
                    total_sessions = validated_data['total_sessions']
                    amount_paid = instance.price * participants * total_sessions
                else:
                    amount_paid = instance.price * participants
                
                print(f"Calculated amount_paid: {amount_paid}")  # Add this
                
                # Create the booking
                booking = Booking.objects.create(
                    **validated_data,
                    amount_paid=amount_paid
                )
                
                print(f"Created booking: {booking.id}")  # Add this
                
                booking.status = 'confirmed'
                booking.payment_status = 'paid'
                booking.save()
                
                # Generate series bookings for recurring or course types
                if booking.enrollment_type in ['Full Course', 'Recurring Classes']:
                    print(f"Generating series bookings for {booking.enrollment_type}")  # Add this
                    booking.generate_series_bookings()
                
                return booking
            except Exception as e:
                print(f"Error creating booking: {str(e)}")  # Add this
                raise

class BookingDetailSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    student_email = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    business_name = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField()
    time = serializers.SerializerMethodField()
    enrollment_details = serializers.SerializerMethodField()
    group_bookings = serializers.SerializerMethodField()
    booking_group_id = serializers.UUIDField(read_only=True)
    
    class Meta:
        model = Booking
        fields = [
            'id', 'schedule_instance', 'date', 'time',
            'student_name', 'student_email', 'class_name', 
            'business_name', 'participants', 'notes', 'status',
            'booking_date', 'cancelled_at', 'cancellation_reason',
            'amount_paid', 'payment_status', 'enrollment_type',
            'enrollment_details', 'attendance_marked', 'attended',
            'booking_group_id', 'group_bookings'
        ]

    def get_group_bookings(self, obj):
        """
        Retrieve all bookings in the same booking group
        """
        if not obj.booking_group_id:
            return []
        
        group_bookings = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).exclude(id=obj.id)
        
        return BookingDetailSerializer(group_bookings, many=True).data
    
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
        if obj.enrollment_type == 'single':
            return None
            
        details = {
            'type': obj.enrollment_type,
            'recurrence_pattern': obj.recurrence_pattern,
            'sessions_per_week': obj.sessions_per_week
        }
        
        if obj.enrollment_type == 'course':
            details.update({
                'start_date': obj.course_start_date,
                'end_date': obj.course_end_date,
                'total_sessions': obj.total_sessions
            })
        else:  # recurring
            details.update({
                'current_period_end': obj.current_period_end
            })
            
        return details