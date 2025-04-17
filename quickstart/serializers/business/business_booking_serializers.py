from rest_framework import serializers
from ...models import Booking, CustomUser 

# Renamed from BookingListSerializer for clarity
class BusinessBookingListSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title')
    option_name = serializers.CharField(source='schedule_instance.schedule.option.title')
    user_name = serializers.SerializerMethodField()
    user_email = serializers.CharField(source='user.email')
    date = serializers.DateField(source='schedule_instance.date')
    time = serializers.TimeField(source='schedule_instance.time')
    duration = serializers.IntegerField(source='schedule_instance.schedule.duration')
    booking_type = serializers.CharField(source='enrollment_type')
    session_info = serializers.SerializerMethodField()

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

    def get_session_info(self, obj):
        if obj.enrollment_type == 'Full Course':
            related_bookings = Booking.objects.filter(
                booking_group_id=obj.booking_group_id
            ).order_by('schedule_instance__date')

            total_sessions = related_bookings.count()
            # Handle potential edge case where obj might not be in the list (e.g., if query changed)
            try:
                 current_session = list(related_bookings).index(obj) + 1
            except ValueError:
                 # Fallback or log error if obj not found in its own group
                 current_session = 'N/A'


            return {
                'current_session': current_session,
                'total_sessions': total_sessions
            }
        return None