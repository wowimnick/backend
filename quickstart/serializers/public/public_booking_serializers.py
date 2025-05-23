# serializers/public/public_booking_serializers.py
from decimal import Decimal
import uuid
from rest_framework import serializers
from django.utils import timezone
# from django.core.exceptions import ValidationError as DjangoValidationError # Not directly used here
from rest_framework.exceptions import ValidationError as DRFValidationError # Use DRF's
from django.db import transaction

# Adjust import path as needed
from ...models import (
    Booking,
    ClassImage,
    Schedule,
    ScheduleInstance,
    CustomUser,
    Reviews
)

import logging
logger = logging.getLogger(__name__)

# --- Booking Create Serializer (Used by Students and Payment Intent Creation) ---
class BookingCreateSerializer(serializers.Serializer):
    selectedSlots = serializers.ListField(
        child=serializers.DictField(),
        min_length=1
    )
    notes = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)
    participants = serializers.IntegerField(default=1, min_value=1, max_value=4) # Max value from existing model
    participant_details = serializers.ListField(
        child=serializers.DictField(),
        required=False, # Made optional; validation logic handles based on participants count
        allow_empty=True # Allow empty list if participants=0 or if not provided for single participant
    )

    def validate_selectedSlots(self, value):
        if not value:
            raise DRFValidationError('At least one slot must be selected')

        first_slot = value[0]
        slot_id = first_slot.get('id')

        if not slot_id:
            raise DRFValidationError('Slot ID is required for the first selected slot.')

        try:
            instance = ScheduleInstance.objects.select_related(
                'schedule',
                'schedule__option', # For booking_type
                'schedule__option__classId__businessId' # For business_timezone later
            ).get(id=slot_id)

            if instance.status != 'scheduled':
                raise DRFValidationError('This session is not available for booking.')

            if instance.date < timezone.now().date():
                raise DRFValidationError('Cannot book past sessions.')
            
            self.context['validated_instance'] = instance
            return value

        except ScheduleInstance.DoesNotExist:
            raise DRFValidationError('Invalid schedule instance ID.')
        except (ValueError, TypeError):
             raise DRFValidationError('Invalid slot ID format.')


    def validate(self, data):
        instance = self.context.get('validated_instance')
        if not instance:
            raise DRFValidationError("Schedule instance validation failed unexpectedly. Please ensure selectedSlots are valid.")

        participants_count = data.get('participants', 1)
        participant_details = data.get('participant_details', [])

        if participants_count > 0 :
            if not isinstance(participant_details, list):
                raise DRFValidationError({'participant_details': 'Must be a list.'})
            if not participant_details and participants_count > 1:
                 raise DRFValidationError({
                    'participant_details': 'Participant details are required for group bookings.'
                 })
            if participant_details:
                if len(participant_details) != participants_count:
                    raise DRFValidationError({
                        'participant_details': f'Number of participant details ({len(participant_details)}) must match participants count ({participants_count}).'
                    })
                for i, detail in enumerate(participant_details):
                    if not isinstance(detail, dict):
                        raise DRFValidationError({
                            'participant_details': f'Item {i+1} must be a dictionary.'
                        })
                    name_value = detail.get('name')
                    if not name_value or not isinstance(name_value, str) or not name_value.strip():
                        raise DRFValidationError({
                            'participant_details': f'Participant {i+1} must have a non-empty name.'
                        })

        if not instance.can_accommodate(participants_count):
            raise DRFValidationError({'participants': 'Not enough spots available for the selected session.'})

        if instance.schedule.option.booking_type == 'Full Course':
            future_instances = ScheduleInstance.objects.filter(
                schedule=instance.schedule,
                date__gte=instance.date,
                status='scheduled'
            ).order_by('date')

            if not future_instances.exists():
                raise DRFValidationError({'selectedSlots': 'No future sessions found for this course starting from the selected date.'})

            self.context['future_course_instances'] = future_instances

            for future_instance_item in future_instances:
                if not future_instance_item.can_accommodate(participants_count):
                    raise DRFValidationError({
                        'participants': f'Not enough spots available for the course session on {future_instance_item.date}. Course cannot be booked with {participants_count} participants.'
                    })
        return data

    def create(self, validated_data):
        user = self.context['request'].user
        initial_instance = self.context['validated_instance']
        participants_count = validated_data['participants']
        participant_details_data = validated_data.get('participant_details', [])
        notes = validated_data.get('notes', '')

        price_per_instance = initial_instance.price
        booking_type = initial_instance.schedule.option.booking_type
        enrollment_type = 'Full Course' if booking_type == 'Full Course' else 'Single Session'

        try:
            with transaction.atomic():
                if booking_type == 'Full Course':
                    booking_group_id = uuid.uuid4()
                    course_instances = self.context.get('future_course_instances')
                    if not course_instances:
                         raise DRFValidationError("Course instances not found during creation (serializer.create).")

                    bookings = []
                    total_course_price = Decimal('0.00')

                    for instance_item in course_instances:
                        instance_price = instance_item.price * participants_count
                        total_course_price += instance_price

                        booking = Booking(
                            schedule_instance=instance_item,
                            user=user,
                            booking_group_id=booking_group_id,
                            participants=participants_count,
                            participant_details=participant_details_data,
                            notes=notes,
                            amount_paid=instance_price,
                            status='pending',
                            payment_status='pending',
                            enrollment_type=enrollment_type
                        )
                        bookings.append(booking)

                    created_bookings = Booking.objects.bulk_create(bookings)
                    logger.info(f"BookingCreateSerializer: Pending Course Booking created (Group: {booking_group_id}) for User {user.email}. Total Price: {total_course_price}")
                    # For response consistency, return the first booking of the course
                    return created_bookings[0] if created_bookings else None
                else: # Single Session
                    single_session_price = price_per_instance * participants_count
                    booking = Booking.objects.create(
                        schedule_instance=initial_instance,
                        user=user,
                        participants=participants_count,
                        participant_details=participant_details_data,
                        notes=notes,
                        amount_paid=single_session_price,
                        status='pending',
                        payment_status='pending',
                        enrollment_type=enrollment_type
                    )
                    logger.info(f"BookingCreateSerializer: Pending Single Session Booking created (ID: {booking.id}) for User {user.email}. Price: {single_session_price}")
                    return booking

        except Exception as e:
            logger.error(f"Error creating booking (serializer.create) for user {user.email}: {str(e)}", exc_info=True)
            raise DRFValidationError({"error": "Failed to create booking. Please try again."})

# --- BookingDetailSerializer ---
class BookingDetailSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration', read_only=True)
    user_name = serializers.SerializerMethodField(read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    course_details = serializers.SerializerMethodField(read_only=True)
    related_bookings = serializers.SerializerMethodField(read_only=True)
    business_timezone = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.business_timezone', read_only=True) # Added


    class Meta:
        model = Booking
        fields = [
            'id', 'date', 'time', 'duration',
            'user_name', 'user_email', 'class_name',
            'business_name', 'participants', 'participant_details',
            'notes', 'status', 'booking_date', 'cancelled_at',
            'cancellation_reason', 'amount_paid',
            'payment_status', 'enrollment_type',
            'attendance_marked', 'attended',
            'booking_group_id',
            'course_details',
            'related_bookings',
            'business_timezone' # Added
        ]
        read_only_fields = fields

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_course_details(self, obj):
        if obj.enrollment_type != 'Full Course' or not obj.booking_group_id:
            return None
        try:
            schedule = obj.schedule_instance.schedule
            total_sessions = ScheduleInstance.objects.filter(schedule=schedule).count()
            return {
                'start_date': schedule.start_date,
                'end_date': schedule.end_date,
                'day': schedule.day,
                'time': schedule.time,
                'total_sessions': total_sessions
            }
        except AttributeError:
             return None

    def get_related_bookings(self, obj):
        if not obj.booking_group_id:
            return None
        related = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).exclude(id=obj.id).select_related(
             'schedule_instance'
        ).order_by('schedule_instance__date', 'schedule_instance__time')
        return [{
            'id': booking.id,
            'date': booking.schedule_instance.date,
            'time': booking.schedule_instance.time,
            'status': booking.status,
            'attended': booking.attended,
            'attendance_marked': booking.attendance_marked
        } for booking in related]

# --- StudentBookingSerializer ---
class StudentBookingSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True) # Naive business local date
    time = serializers.TimeField(source='schedule_instance.time', read_only=True) # Naive business local time
    coordinates = serializers.CharField(source='schedule_instance.schedule.option.classId.coordinates', allow_null=True, read_only=True)
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName', read_only=True)
    price = serializers.DecimalField(source='amount_paid', max_digits=10, decimal_places=2, read_only=True)
    class_image = serializers.SerializerMethodField(read_only=True)
    booking_id = serializers.IntegerField(source='id', read_only=True)
    has_review = serializers.SerializerMethodField(read_only=True)
    enrollment_type = serializers.CharField(read_only=True)
    session_info = serializers.SerializerMethodField(read_only=True)
    class_id = serializers.IntegerField(source='schedule_instance.schedule.option.classId.classId', read_only=True)
    business_timezone = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.business_timezone', read_only=True) 
    cancellation_policy = serializers.CharField(source='schedule_instance.schedule.option.cancellationPolicy', read_only=True)

    class Meta:
        model = Booking
        fields = [
            'booking_id',
            'class_id',
            'class_name', 'option_name', 'date', 'time',
            'coordinates', 'business_name', 'price', 'status',
            'class_image', 'has_review', 'session_info', 'enrollment_type',
            'participants', 'participant_details',
            'payment_status',
            'cancellation_reason',
            'business_timezone', 'cancellation_policy'
        ]
        read_only_fields = fields

    def get_class_image(self, obj):
        try:
            class_obj = obj.schedule_instance.schedule.option.classId
            if hasattr(class_obj, 'prefetched_images') and class_obj.prefetched_images:
                image_instance = class_obj.prefetched_images[0]
            else:
                image_instance = class_obj.images.order_by('createdAt').first()

            if image_instance and image_instance.image and hasattr(image_instance.image, 'url'):
                return image_instance.image.url
            return None
        except (AttributeError, ValueError, TypeError):
            return None

    def get_has_review(self, obj):
        try:
            # Check if obj.review exists and is not None
            return hasattr(obj, 'review') and obj.review is not None
        except Reviews.DoesNotExist: # This exception might not be needed if using obj.review directly
            return False
        except Exception: # Catch any other potential errors
            return False


    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course' and obj.booking_group_id:
            related_bookings_qs = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date', 'schedule_instance__time')

            total_sessions = related_bookings_qs.count()
            current_session_num = 'N/A'
            for index, booking_in_course in enumerate(related_bookings_qs):
                if booking_in_course.id == obj.id:
                    current_session_num = index + 1
                    break
            
            if current_session_num == 'N/A':
                 logger.warning(f"StudentBookingSerializer: Booking ID {obj.id} not found within its own group {obj.booking_group_id}.")

            return {
                'current_session': current_session_num,
                'total_sessions': total_sessions
            }
        return None