# serializers/student/student_booking_serializers.py
from decimal import Decimal
import uuid
from rest_framework import serializers
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db import transaction

# Adjust import path as needed
from ...models import (
    Booking,
    ClassImage,
    Schedule,
    ScheduleInstance,
    CustomUser,
    Reviews # Added Reviews model
)

import logging
logger = logging.getLogger(__name__)

# --- Booking Create Serializer (Used by Students) ---
class BookingCreateSerializer(serializers.Serializer):
    selectedSlots = serializers.ListField(
        child=serializers.DictField(),
        min_length=1
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    participants = serializers.IntegerField(default=1, min_value=1, max_value=4) # Keep max_value for now

    def validate_selectedSlots(self, value):
        if not value:
            raise serializers.ValidationError('At least one slot must be selected')

        first_slot = value[0]
        slot_id = first_slot.get('id')

        if not slot_id:
            raise serializers.ValidationError('Slot ID is required')

        try:
            # Ensure instance data is fresh and check status/availability
            instance = ScheduleInstance.objects.select_related(
                'schedule',
                'schedule__option'
            ).get(id=slot_id)

            if instance.status != 'scheduled':
                raise serializers.ValidationError('This session is not available for booking')

            if instance.date < timezone.now().date():
                raise serializers.ValidationError('Cannot book past sessions')

            participants = self.context['request'].data.get('participants', 1)
            if not instance.can_accommodate(participants):
                raise serializers.ValidationError('Not enough spots available for the selected session.')

            # Store the validated instance for use in validate() and create()
            self.context['validated_instance'] = instance
            return value

        except ScheduleInstance.DoesNotExist:
            raise serializers.ValidationError('Invalid schedule instance ID.')
        except ValueError: # If slot_id isn't a valid format
             raise serializers.ValidationError('Invalid slot ID format.')


    def validate(self, data):
        instance = self.context.get('validated_instance')
        if not instance:
            # This shouldn't happen if validate_selectedSlots runs first, but safety check
            raise serializers.ValidationError("Schedule instance validation failed unexpectedly.")

        participants = data.get('participants', 1) # Use participants from data

        # For course bookings, validate all future instances have space
        if instance.schedule.option.booking_type == 'Full Course':
            future_instances = ScheduleInstance.objects.filter(
                schedule=instance.schedule,
                date__gte=instance.date, # From the initial instance onwards
                # date__lte=instance.schedule.end_date, # No need to check end_date here, filter by schedule is enough
                status='scheduled' # Only check scheduled ones
            ).order_by('date') # Order is important

            # Store future instances in context for create method
            self.context['future_course_instances'] = future_instances

            for future_instance in future_instances:
                if not future_instance.can_accommodate(participants):
                    raise serializers.ValidationError({
                        'participants': f'Not enough spots available for the session on {future_instance.date}. Course cannot be booked.'
                    })
        else:
            # Re-check single session availability (though done in validate_selectedSlots, belt-and-suspenders)
            if not instance.can_accommodate(participants):
                raise serializers.ValidationError({
                    'participants': 'Not enough spots available for the requested number of participants.'
                })

        return data

    def create(self, validated_data):
        user = self.context['request'].user
        initial_instance = self.context['validated_instance']
        participants = validated_data['participants']
        notes = validated_data.get('notes', '')

        # Use instance properties directly
        price_per_instance = initial_instance.price
        booking_type = initial_instance.schedule.option.booking_type
        enrollment_type = 'Full Course' if booking_type == 'Full Course' else 'Single Session'

        try:
            with transaction.atomic():
                if booking_type == 'Full Course':
                    booking_group_id = uuid.uuid4()
                    # Use future instances stored in context during validation
                    course_instances = self.context.get('future_course_instances')
                    if not course_instances: # Safety check if context missing
                         raise serializers.ValidationError("Course instances not found during creation.")

                    bookings = []
                    total_course_price = Decimal('0.00') # Calculate total course price

                    for instance in course_instances:
                        # Price for course booking could be different, fetch from option maybe?
                        # For now, assume price_per_instance is correct for each session of the course.
                        instance_price = instance.price * participants
                        total_course_price += instance_price

                        # Create booking for each instance in the course
                        booking = Booking(
                            schedule_instance=instance,
                            user=user,
                            booking_group_id=booking_group_id,
                            participants=participants,
                            notes=notes, # Note applies to the whole booking group
                            amount_paid=instance_price, # Store price per instance
                            status='pending', # Start as pending, confirm after payment
                            payment_status='pending',
                            enrollment_type=enrollment_type
                        )
                        bookings.append(booking)

                    # Bulk create for efficiency
                    created_bookings = Booking.objects.bulk_create(bookings)

                    # TODO: Initiate payment process for total_course_price here
                    # The status remains 'pending' until payment is confirmed via webhook
                    logger.info(f"Pending Course Booking created (Group: {booking_group_id}) for User {user.email}. Total Price: {total_course_price}")

                    # Return the first booking as representative
                    return created_bookings[0]
                else:
                    # Single session booking
                    single_session_price = price_per_instance * participants
                    booking = Booking.objects.create(
                        schedule_instance=initial_instance,
                        user=user,
                        participants=participants,
                        notes=notes,
                        amount_paid=single_session_price,
                        status='pending', # Start as pending
                        payment_status='pending',
                        enrollment_type=enrollment_type
                    )

                    # TODO: Initiate payment process for single_session_price here
                    logger.info(f"Pending Single Session Booking created (ID: {booking.id}) for User {user.email}. Price: {single_session_price}")

                    return booking

        except Exception as e:
            logger.error(f"Error creating booking for user {user.email}: {str(e)}", exc_info=True)
            # Raise a DRF validation error to give feedback to the client
            raise DRFValidationError({"error": "Failed to create booking. Please try again."})

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
            'booking_group_id',
            'course_details', # Added from original logic
            'related_bookings' # Added from original logic
        ]
        read_only_fields = fields # Make all fields read-only by default in detail view

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

    def get_course_details(self, obj):
        """Return course details if this is a course booking"""
        if obj.enrollment_type != 'Full Course' or not obj.booking_group_id:
            return None

        # Efficiently fetch schedule info if needed
        try:
            schedule = obj.schedule_instance.schedule
            # Avoid N+1: Count instances related to this schedule
            total_sessions = ScheduleInstance.objects.filter(
                schedule=schedule
            ).count()

            return {
                'start_date': schedule.start_date,
                'end_date': schedule.end_date,
                'day': schedule.day,
                'time': schedule.time,
                'total_sessions': total_sessions
            }
        except AttributeError: # Handle potential missing relations
             return None


    def get_related_bookings(self, obj):
        """Get other bookings in the same course group"""
        if not obj.booking_group_id:
            return None

        # Query related bookings, excluding self, optimized
        related = Booking.objects.filter(
            booking_group_id=obj.booking_group_id
        ).exclude(id=obj.id).select_related(
             'schedule_instance' # Avoid N+1 for date/time
        ).order_by('schedule_instance__date', 'schedule_instance__time')

        # Serialize necessary fields efficiently
        return [{
            'id': booking.id,
            'date': booking.schedule_instance.date,
            'time': booking.schedule_instance.time,
            'status': booking.status,
            'attended': booking.attended,
            'attendance_marked': booking.attendance_marked
        } for booking in related]
    
# --- Student Booking List Serializer ---
class StudentBookingSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    coordinates = serializers.CharField(source='schedule_instance.schedule.option.classId.coordinates', allow_null=True) # Allow null coords
    business_name = serializers.CharField(source='schedule_instance.schedule.option.classId.businessId.businessName')
    price = serializers.DecimalField(source='amount_paid', max_digits=10, decimal_places=2)
    class_image = serializers.SerializerMethodField()
    booking_id = serializers.IntegerField(source='id')
    has_review = serializers.SerializerMethodField()
    enrollment_type = serializers.CharField()
    session_info = serializers.SerializerMethodField()
    class_id = serializers.IntegerField(source='schedule_instance.schedule.option.classId.classId') # Get the actual classId PK

    class Meta:
        model = Booking
        fields = [
            'booking_id',
            'class_id', 
            'class_name', 'option_name', 'date', 'time',
            'coordinates', 'business_name', 'price', 'status',
            'class_image', 'has_review', 'session_info', 'enrollment_type',
            'payment_status',
            'cancellation_reason'
        ]

    def get_class_image(self, obj):
        try:
            class_obj = obj.schedule_instance.schedule.option.classId
            images = class_obj.images.all() # Access images related to the class
            if images:
                # Check if image field exists and has a URL
                if images[0].image and hasattr(images[0].image, 'url'):
                    return images[0].image.url
                else:
                    logger.debug(f"Image object found for class {class_obj.classId}, but image field is missing or has no URL.")
                    return None
            else:
                 return None

        except (AttributeError, ValueError, TypeError) as e: # Catch potential errors accessing nested relations or URL
            logger.warning(f"Error getting class image for booking {obj.id}: {e}")
            return None

    def get_has_review(self, obj):
        try:
            return hasattr(obj, 'review') and obj.review is not None
        except Reviews.DoesNotExist:
            return False
        except Exception as e:
            logger.debug(f"Error checking review existence for booking {obj.id}: {e}")
            return False # Default to false on error

    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course' and obj.booking_group_id:
            related_bookings = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date')

            total_sessions = related_bookings.count()
            current_session = 'N/A'
            # Convert queryset to list for index() method
            related_list = list(related_bookings)
            try:
                 # Find the index of the current object within the list
                 current_index = related_list.index(obj)
                 current_session = current_index + 1
            except ValueError:
                 logger.warning(f"Booking ID {obj.id} not found within its own booking group {obj.booking_group_id} during session info calculation.")

            return {
                'current_session': current_session,
                'total_sessions': total_sessions
            }
        return None