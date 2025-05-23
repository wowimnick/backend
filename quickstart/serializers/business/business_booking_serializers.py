from rest_framework import serializers
from ...models import Booking, CustomUser
import logging

logger = logging.getLogger(__name__)

class BusinessBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration')
    session_info = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'user_name', 'user_email', 'class_name',
            'option_name', 'date', 'time', 'duration',
            'participants', 'participant_details',
            'status', 'enrollment_type', 
            'amount_paid', 'payment_status',
            'notes', 'booking_date', 'attendance_marked',
            'attended', 'session_info'
        ]

    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()

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
                 logger.warning(f"Booking ID {obj.id} not found within its own booking group {obj.booking_group_id} (Business View).")

            return {
                'current_session': current_session_num,
                'total_sessions': total_sessions
            }
        return None