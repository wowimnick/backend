"""
Course-related serializers for enrollment, schedule display, and management.
Import these in your main serializers/__init__.py
"""

from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum, Count
from django.db.models.functions import Coalesce
from decimal import Decimal
import uuid

from quickstart.models import (
    CourseEnrollment,
    Schedule,
    ScheduleInstance,
    Booking,
    ClassOption,
)

import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Course Management Serializers (Business Facing)
# ============================================================================

class CourseScheduleManagementSerializer(serializers.ModelSerializer):
    """
    Serializer for Creating and Updating Course Schedules.
    Allows past dates for updates (to fix typos) but strictly validates 
    capacity changes.
    """
    class_option_id = serializers.IntegerField(write_only=True, required=False)
    session_count = serializers.IntegerField(source="instances.count", read_only=True)
    enrolled_students = serializers.SerializerMethodField()

    class Meta:
        model = Schedule
        fields = [
            "id",
            "class_option_id",
            "start_date",
            "end_date",
            "day",
            "time",
            "duration",
            "price",
            "maxParticipants",
            "minParticipants",
            "session_count",
            "enrolled_students",
        ]

    def get_enrolled_students(self, obj):
        return obj.course_enrollments.filter(status__in=["pending", "active"]).count()

    def validate_start_date(self, value):
        """
        Allow past start dates only if:
        1. We are updating an existing record (self.instance is set).
        2. Or if creating a Full Course (optional, but safer to block past for new courses).
        """
        if self.instance:
            return value
        
        # For new courses, strictly require future dates
        if value < timezone.now().date():
             raise serializers.ValidationError("New courses cannot start in the past.")
        
        return value

    def validate_maxParticipants(self, value):
        if self.instance:
            current_enrolled = self.instance.course_enrollments.filter(
                status__in=["pending", "active"]
            ).aggregate(total=Sum('participants'))['total'] or 0
            
            if value < current_enrolled:
                raise serializers.ValidationError(
                    f"Cannot reduce capacity to {value}. There are currently {current_enrolled} enrolled participants."
                )
        return value


class BusinessCourseSerializer(serializers.ModelSerializer):
    """Business view of their courses (Read-Only/List View)"""

    class_title = serializers.CharField(source="option.parent_class_title")
    enrolled_students = serializers.SerializerMethodField()
    total_revenue = serializers.SerializerMethodField()
    session_count = serializers.IntegerField(source="instances.count", read_only=True)

    class Meta:
        model = Schedule
        fields = [
            "id",
            "class_title",
            "start_date",
            "end_date",
            "day",
            "time",
            "duration",
            "price",
            "maxParticipants",
            "session_count",
            "enrolled_students",
            "total_revenue",
        ]

    def get_enrolled_students(self, obj):
        return CourseEnrollment.objects.filter(
            schedule=obj, status__in=["pending", "active"]
        ).count()

    def get_total_revenue(self, obj):
        return CourseEnrollment.objects.filter(
            schedule=obj, status__in=["active", "completed"]
        ).aggregate(total=Sum("total_amount_paid"))["total"] or Decimal("0.00")


# ============================================================================
# Course Schedule Serializers (Public - for browsing)
# ============================================================================


class PublicCourseScheduleSerializer(serializers.Serializer):
    """
    Serializer for publicly viewing available course schedules.
    Now handles aggregated/grouped data from the ViewSet.
    """
    # Define fields to match the annotated queryset
    id = serializers.IntegerField() # This is the representative schedule ID
    option = serializers.IntegerField()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    time = serializers.TimeField()
    duration = serializers.IntegerField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    maxParticipants = serializers.IntegerField()
    minParticipants = serializers.IntegerField()
    days = serializers.ListField(child=serializers.CharField()) # The array of days

    # SerializerMethodFields to get related data and calculate availability
    class_title = serializers.SerializerMethodField()
    enrolled_count = serializers.SerializerMethodField()
    available_spots = serializers.SerializerMethodField()
    session_count = serializers.SerializerMethodField()

    def get_class_title(self, obj):
        # The 'obj' is a dictionary from the .values() queryset
        try:
            # We fetch the related option object using the option ID from the group
            option_obj = ClassOption.objects.get(pk=obj['option'])
            return option_obj.classId.title
        except ClassOption.DoesNotExist:
            return "Unknown Class"

    def get_enrolled_count(self, obj):
        # Sum enrollments from ALL schedules in this group
        return CourseEnrollment.objects.filter(
            schedule__option_id=obj['option'],
            schedule__start_date=obj['start_date'],
            schedule__end_date=obj['end_date'],
            schedule__time=obj['time'],
            status__in=['active', 'pending']
        ).aggregate(
            total_participants=Coalesce(Sum('participants'), 0)
        )['total_participants']

    def get_available_spots(self, obj):
        enrolled = self.get_enrolled_count(obj)
        return obj['maxParticipants'] - enrolled

    def get_session_count(self, obj):
        # Calculate total sessions based on days
        total_sessions = 0
        schedules = Schedule.objects.filter(
            option_id=obj['option'],
            start_date=obj['start_date'],
            end_date=obj['end_date'],
            time=obj['time'],
        )
        for schedule in schedules:
            total_sessions += schedule.instances.count()
        return total_sessions

# ============================================================================
# Course Enrollment Serializers (Student-facing)
# ============================================================================


class CourseEnrollmentSerializer(serializers.ModelSerializer):
    """
    Serializer for student's course enrollment list view.
    Shows overview with progress.
    """

    course_title = serializers.CharField(
        source="schedule.option.parent_class_title", read_only=True
    )
    class_id = serializers.IntegerField(
        source="schedule.option.classId.classId", read_only=True
    )
    business_name = serializers.CharField(
        source="schedule.option.classId.businessId.businessName", read_only=True
    )
    business_id = serializers.IntegerField(
        source="schedule.option.classId.businessId.businessId", read_only=True
    )

    location = serializers.CharField(
        source="schedule.option.classId.location", read_only=True
    )
    city = serializers.CharField(source="schedule.option.classId.city", read_only=True)

    progress_percentage = serializers.IntegerField(read_only=True)
    next_session = serializers.SerializerMethodField()
    booking_reference = serializers.SerializerMethodField()

    class Meta:
        model = CourseEnrollment
        fields = [
            "id",
            "booking_reference",
            "class_id",
            "course_title",
            "business_name",
            "business_id",
            "location",
            "city",
            "status",
            "enrollment_date",
            "sessions_completed",
            "total_sessions",
            "progress_percentage",
            "total_amount_paid",
            "participants",
            "next_session",
        ]
        read_only_fields = fields

    def get_booking_reference(self, obj):
        """Get the reference from the first session booking"""
        first_booking = obj.all_session_bookings.first()
        return first_booking.user_facing_reference if first_booking else None

    def get_next_session(self, obj):
        """Get next upcoming session details"""
        next_booking = obj.next_session
        if not next_booking:
            return None

        instance = next_booking.schedule_instance
        return {
            "session_number": next_booking.course_session_number,
            "date": instance.date,
            "time": instance.time,
            "duration": instance.duration,
            "location": obj.schedule.option.classId.location,
        }


class CourseEnrollmentDetailSerializer(CourseEnrollmentSerializer):
    """
    Extended serializer with all session details for enrollment detail view.
    """

    all_sessions = serializers.SerializerMethodField()
    cancellation_eligible = serializers.SerializerMethodField()

    class Meta(CourseEnrollmentSerializer.Meta):
        fields = CourseEnrollmentSerializer.Meta.fields + [
            "all_sessions",
            "cancellation_eligible",
            "cancellation_policy",
            "cancellation_custom_hours",
            "cancellation_refund_percentage",
        ]

    def get_all_sessions(self, obj):
        """Get all course sessions with attendance status"""
        bookings = obj.all_session_bookings

        return [
            {
                "session_number": booking.course_session_number,
                "booking_id": booking.id,
                "date": booking.schedule_instance.date,
                "time": booking.schedule_instance.time,
                "duration": booking.schedule_instance.duration,
                "status": booking.status,
                "attended": booking.status == "completed",
            }
            for booking in bookings
        ]

    def get_cancellation_eligible(self, obj):
        """Check if course can be cancelled based on policy and first session"""
        if obj.status not in ["pending", "active"]:
            return False

        next_session = obj.next_session
        if not next_session:
            return False

        # Check cancellation policy using same logic as single sessions
        return self._check_cancellation_policy(
            next_session.schedule_instance,
            obj.cancellation_policy,
            obj.cancellation_custom_hours,
        )

    def _check_cancellation_policy(self, schedule_instance, policy, custom_hours):
        """Check if cancellation is allowed based on policy"""
        from datetime import timedelta
        import pytz

        if policy == "strict":
            return False

        business = schedule_instance.schedule.option.classId.businessId
        business_tz = pytz.timezone(business.business_timezone)

        instance_datetime = timezone.datetime.combine(
            schedule_instance.date, schedule_instance.time
        )
        instance_datetime_aware = business_tz.localize(instance_datetime)
        now_aware = timezone.now().astimezone(business_tz)

        if instance_datetime_aware <= now_aware:
            return False

        if policy == "custom":
            required_hours = custom_hours or 0
        else:
            policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
            required_hours = policy_hours_map.get(policy, 0)

        deadline = instance_datetime_aware - timedelta(hours=required_hours)
        return now_aware < deadline


# ============================================================================
# Course Booking/Enrollment Creation Serializer
# ============================================================================


class CourseBookingCreateSerializer(serializers.Serializer):
    """
    Serializer for creating a course enrollment.
    Validates schedule, capacity, and creates all bookings.
    """

    schedule_id = serializers.IntegerField()
    participants = serializers.IntegerField(min_value=1)
    participant_details = serializers.ListField(
        child=serializers.DictField(), required=False, allow_empty=True
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate_schedule_id(self, value):
        """Validate schedule exists and is a course"""
        try:
            schedule = Schedule.objects.select_related(
                "option__classId__businessId"
            ).get(id=value)

            if schedule.option.booking_type != "Full Course":
                raise serializers.ValidationError(
                    "This schedule is not configured for course bookings."
                )

            return value
        except Schedule.DoesNotExist:
            raise serializers.ValidationError("Course schedule not found.")

    def validate_participants(self, value):
        """Validate participant count"""
        if value < 1:
            raise serializers.ValidationError("Must have at least 1 participant.")
        if value > 10:
            raise serializers.ValidationError("Maximum 10 participants per enrollment.")
        return value

    def validate_participant_details(self, value):
        """Validate participant details structure"""
        if not value:
            return value

        for i, detail in enumerate(value):
            if not isinstance(detail, dict):
                raise serializers.ValidationError(
                    f"Participant {i+1} must be a dictionary."
                )

            name = detail.get("name")
            if not name or not isinstance(name, str) or not name.strip():
                raise serializers.ValidationError(
                    f"Participant {i+1} must have a non-empty name."
                )

        return value

    def validate(self, data):
        """
        Validate course availability and capacity for ALL sessions.
        Uses database lock to prevent race conditions.
        """
        with transaction.atomic():
            # Lock the schedule to prevent race conditions
            schedule = Schedule.objects.select_for_update().get(id=data["schedule_id"])

            # Get all future course instances
            instances = list(
                schedule.instances.filter(
                    status="scheduled", date__gte=timezone.now().date()
                ).order_by("date", "time")
            )

            if not instances:
                raise serializers.ValidationError(
                    "This course has no available sessions."
                )

            # Check if course has already started
            first_instance = instances[0]
            if first_instance.date < timezone.now().date():
                raise serializers.ValidationError(
                    "This course has already started and cannot accept new enrollments."
                )

            # Validate capacity for ALL sessions
            participants = data["participants"]
            for instance in instances:
                if not instance.can_accommodate(participants):
                    raise serializers.ValidationError(
                        f"Session on {instance.date} does not have enough capacity. "
                        f"Available: {instance.available_spots}, Requested: {participants}"
                    )

            # Validate participant details match count
            participant_details = data.get("participant_details", [])
            if participant_details and len(participant_details) != participants:
                raise serializers.ValidationError(
                    f"Number of participant details ({len(participant_details)}) "
                    f"must match participants count ({participants})."
                )

            # Store validated objects for use in view
            self.context["validated_schedule"] = schedule
            self.context["validated_instances"] = instances

            return data