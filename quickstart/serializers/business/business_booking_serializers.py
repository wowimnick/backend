from rest_framework import serializers
from ...models import Booking, CustomUser, ScheduleInstance, Schedule, ClassOption, ClassesMain, BusinessInfo # Ensure all models are imported
import logging

logger = logging.getLogger(__name__)

# --- Serializer for LISTING bookings (Business Context) ---
class BusinessBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title', read_only=True)
    user_name = serializers.SerializerMethodField(read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration', read_only=True) # From Schedule model via instance
    session_info = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Booking
        fields = [
            'id', 'user_name', 'user_email', 'class_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'participant_details', # Keep these for list if useful for quick view
            'status', 'enrollment_type',
            'amount_paid', 'payment_status',
            'notes', 'booking_date', 'attendance_marked',
            'attended', 'session_info'
        ]
        read_only_fields = fields # All fields are read-only in list view

    def get_user_name(self, obj):
        if obj.user: # Check if user object exists
            return f"{obj.user.first_name} {obj.user.last_name}".strip()
        return "Unknown User"

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
            
            if current_session_num == 'N/A' and total_sessions > 0:
                 logger.warning(f"BusinessBookingListSerializer: Booking ID {obj.id} not found within its own group {obj.booking_group_id}.")

            return {
                'current_session': current_session_num,
                'total_sessions': total_sessions
            }
        return None
class _BusinessDetailUserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    class Meta:
        model = CustomUser
        fields = ['userId', 'email', 'full_name', 'phone_number', 'avatar_url']

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip()

    def get_avatar_url(self, obj):
        return obj.get_avatar_url()

class _BusinessDetailClassSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassesMain
        fields = ['classId', 'title']

class _BusinessDetailOptionSerializer(serializers.ModelSerializer):
    class_info = _BusinessDetailClassSerializer(source='classId', read_only=True)
    class Meta:
        model = ClassOption
        fields = ['optionId', 'title', 'booking_type', 'class_info', 'cancellationPolicy'] # Added cancellationPolicy

class _BusinessDetailScheduleSerializer(serializers.ModelSerializer):
    # We don't need full option_info here, just what's relevant for the instance context
    option_booking_type = serializers.CharField(source='option.booking_type', read_only=True)
    option_cancellation_policy = serializers.CharField(source='option.cancellationPolicy', read_only=True)
    class Meta:
        model = Schedule
        fields = ['id', 'day', 'time', 'duration', 'option_booking_type', 'option_cancellation_policy'] # Removed full option_info

class _BusinessDetailScheduleInstanceSerializer(serializers.ModelSerializer):
    schedule_details = _BusinessDetailScheduleSerializer(source='schedule', read_only=True)
    class Meta:
        model = ScheduleInstance
        fields = ['id', 'date', 'time', 'duration', 'price', 'max_participants', 'schedule_details']

class _BusinessContextSerializer(serializers.ModelSerializer): # For business context for this booking
    class Meta:
        model = BusinessInfo
        fields = ['businessName', 'business_timezone']


class BusinessBookingDetailSerializer(serializers.ModelSerializer):
    user_details = _BusinessDetailUserSerializer(source='user', read_only=True)
    schedule_instance_details = _BusinessDetailScheduleInstanceSerializer(source='schedule_instance', read_only=True)
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title', read_only=True)
    date = serializers.DateField(source='schedule_instance.date', read_only=True)
    time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration', read_only=True)

    business_context = _BusinessContextSerializer(source='schedule_instance.schedule.option.classId.businessId', read_only=True)
    session_info = serializers.SerializerMethodField(read_only=True) # Reusing from ListSerializer

    class Meta:
        model = Booking
        fields = [
            'id',
            'booking_group_id', # Useful for course context
            'user_details',     # Detailed user info
            
            # Direct fields (can be phased out if frontend uses nested structure)
            'class_name',
            'option_name',
            'date',
            'time',
            'duration',

            'schedule_instance_details', # Structured schedule and class hierarchy
            
            'enrollment_type',
            'status',
            'booking_date', # When the booking was made
            'participants',
            'participant_details',
            'notes',
            'cancelled_at',
            'cancellation_reason',
            'amount_paid',
            'payment_status',
            'attendance_marked',
            'attended',
            'session_info',
            'business_context', # To get business name and timezone
        ]
        read_only_fields = fields # All fields are read-only

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
            if current_session_num == 'N/A' and total_sessions > 0:
                 logger.warning(f"BusinessBookingDetailSerializer: Booking ID {obj.id} not found within its own group {obj.booking_group_id}.")
            return {'current_session': current_session_num, 'total_sessions': total_sessions}
        return None