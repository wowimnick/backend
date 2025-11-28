"""
Views for course enrollment and management.
Students can browse courses, enroll, view progress, and cancel.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError
from decimal import Decimal
from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Min, Sum
from django.db.models.functions import Coalesce
import uuid
import datetime

from quickstart.utils.permissions import CanManageOwnClasses
from quickstart.models import (
    ClassOption,
    CourseEnrollment,
    Booking,
    Schedule,
    ScheduleInstance,
)
from quickstart.serializers.business.business_course_serializers import (
    PublicCourseScheduleSerializer,
    CourseEnrollmentSerializer,
    CourseEnrollmentDetailSerializer,
    CourseBookingCreateSerializer,
    CourseScheduleManagementSerializer,
    BusinessCourseSerializer,
)

import logging

logger = logging.getLogger(__name__)


class BusinessCourseManagementViewSet(viewsets.ModelViewSet):
    """
    Business owners can create and manage their courses.

    Endpoints:
    - GET /api/business/course-management/ - List my courses
    - POST /api/business/course-management/ - Create new course
    - GET /api/business/course-management/{id}/ - Course details with enrollments
    - PUT/PATCH /api/business/course-management/{id}/ - Update course
    - DELETE /api/business/course-management/{id}/ - Delete course
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get_queryset(self):
        """Get courses for user's businesses"""
        user = self.request.user
        # Get businesses where user is owner or staff
        return (
            Schedule.objects.filter(
                option__booking_type="Full Course",
                option__classId__businessId__owner=user,
            )
            .select_related("option__classId__businessId")
            .order_by("-start_date")
        )

    def get_serializer_class(self):
        if self.action in ["create", "update", "partial_update"]:
            return CourseScheduleManagementSerializer
        return BusinessCourseSerializer

    def create(self, request, *args, **kwargs):
        """
        Create a new course schedule.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        class_option_id = data.get("class_option_id")

        with transaction.atomic():
            # Validate option exists and is a course
            try:
                option = ClassOption.objects.select_for_update().get(
                    optionId=class_option_id
                )
                if option.booking_type != "Full Course":
                    return Response(
                        {"error": "Class option must be configured for Full Course"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except ClassOption.DoesNotExist:
                return Response(
                    {"error": "Class option not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Create schedule
            schedule = Schedule.objects.create(
                option=option,
                start_date=data.get("start_date"),
                end_date=data.get("end_date"),
                day=data.get("day"),
                time=data.get("time"),
                duration=data.get("duration", 60),
                price=data.get("price"),
                maxParticipants=data.get("maxParticipants", 15),
                minParticipants=data.get("minParticipants", 1),
            )

            # Generate all course instances
            # Note: Model method might generate past instances if start_date is in past.
            # This is acceptable for "Full Course" if businesses are backfilling data or adding a day mid-course.
            instances = schedule.generate_course_instances()

            # Update course session count
            option.course_session_count = len(instances)
            option.save(update_fields=["course_session_count"])

            logger.info(
                f"Created course schedule {schedule.id} with {len(instances)} sessions"
            )

            return Response(
                {
                    "id": schedule.id,
                    "session_count": len(instances),
                    "message": f"Course created with {len(instances)} sessions",
                },
                status=status.HTTP_201_CREATED,
            )

    def update(self, request, *args, **kwargs):
        """
        Update an existing course schedule.
        SAFETY ENFORCEMENT:
        - If bookings exist: Block Day, Time, Start Date, End Date, Duration changes.
        - Allowed updates: Name, Price, Max Participants (increase only).
        """
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        
        has_active_bookings = instance.bookings.filter(status='confirmed').exists()
        
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            # 1. ENFORCE LOCKDOWN
            if has_active_bookings:
                # Check for attempts to change structural fields
                errors = {}
                
                # Check Dates
                if 'start_date' in data and data['start_date'] != instance.start_date:
                    errors['start_date'] = "Cannot change start date of a course with active students."
                
                if 'end_date' in data and data['end_date'] != instance.end_date:
                    # Specifically blocking shortening OR extending to keep it simple and safe as requested
                    errors['end_date'] = "Cannot change end date of a course with active students."

                # Check Day
                if 'day' in data and data['day'] != instance.day:
                    errors['day'] = "Cannot change the day of a course with active students."
                
                # Check Time
                if 'time' in data and data['time'] != instance.time:
                    errors['time'] = "Cannot change the time of a course with active students."

                if errors:
                    raise ValidationError(errors)

            # 2. Perform the update (for allowed fields like Price, Name, Capacity)
            self.perform_update(serializer)
            instance.refresh_from_db()

            # 3. Propagate Price/Capacity changes to FUTURE instances
            # Even if locked, we allow changing price for future drop-ins or new enrollments
            future_instances = instance.instances.filter(date__gte=timezone.now().date(), status='scheduled')
            for inst in future_instances:
                changed = False
                if inst.price != instance.price:
                    inst.price = instance.price
                    changed = True
                
                # Capacity is already validated in serializer to be >= current enrollment
                if inst.max_participants != instance.maxParticipants:
                    inst.max_participants = instance.maxParticipants
                    changed = True
                
                if changed:
                    inst.save()

        return Response(serializer.data)

    def perform_destroy(self, instance):
        """
        Prevent deletion if there are active bookings.
        """
        if instance.bookings.filter(status='confirmed').exists():
            raise ValidationError("Cannot delete a course schedule with confirmed bookings. Please cancel the course instead.")
        instance.delete()

    @action(detail=True, methods=["get"])
    def enrollments(self, request, pk=None):
        """Get all enrollments for this course"""
        schedule = self.get_object()

        enrollments = (
            CourseEnrollment.objects.filter(schedule=schedule)
            .select_related("user", "contact")
            .order_by("-enrollment_date")
        )

        serializer = CourseEnrollmentSerializer(enrollments, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def cancel_course(self, request, pk=None):
        """
        Cancel entire course (business-initiated).
        Refunds all enrollments.
        """
        schedule = self.get_object()
        reason = request.data.get("reason", "Course cancelled by business")

        with transaction.atomic():
            # Get all active enrollments
            enrollments = CourseEnrollment.objects.filter(
                schedule=schedule, status__in=["pending", "active"]
            )

            for enrollment in enrollments:
                enrollment.status = "cancelled"
                enrollment.cancelled_at = timezone.now()
                enrollment.cancellation_reason = reason
                enrollment.save()

                # Cancel all bookings
                Booking.objects.filter(
                    booking_group_id=enrollment.booking_group_id
                ).update(
                    status="cancelled",
                    cancelled_at=timezone.now(),
                    cancellation_reason=reason,
                    payment_status="refund_pending",
                )

            logger.info(
                f"Cancelled course {schedule.id}, affected {enrollments.count()} enrollments"
            )

            return Response(
                {
                    "message": f"Course cancelled. {enrollments.count()} students will be refunded."
                }
            )


# ============================================================================
# Public Course Browsing (no auth required)
# ============================================================================


class PublicCourseViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for browsing available courses (public access).
    Students can view course details before enrolling.
    """

    serializer_class = PublicCourseScheduleSerializer
    permission_classes = [AllowAny]
    pagination_class = None

    def get_queryset(self):
        """
        Groups schedules that belong to the same course offering.
        """
        base_queryset = (
            Schedule.objects.filter(
                option__booking_type="Full Course",
                option__classId__businessId__isActive=True,
                start_date__isnull=False,
                end_date__isnull=False,
                # We generally only show courses that haven't fully finished yet
                end_date__gte=timezone.now().date(),
            )
            .select_related("option__classId__businessId", "option")
            .order_by("start_date", "time")
        )

        # Optional filtering by class_id or business_id
        class_id = self.request.query_params.get("class_id")
        if class_id:
            base_queryset = base_queryset.filter(option__classId__classId=class_id)

        business_id = self.request.query_params.get("business_id")
        if business_id:
            base_queryset = base_queryset.filter(
                option__classId__businessId__businessId=business_id
            )

        # Group by these fields and aggregate the days and schedule IDs
        grouping_fields = [
            'option',
            'start_date',
            'end_date',
            'time',
            'duration',
            'price',
            'maxParticipants',
            'minParticipants',
        ]

        # Group by these fields and aggregate the days and schedule IDs
        grouped_queryset = base_queryset.values(*grouping_fields).annotate(
            # Collect all days of the week into an array
            days=ArrayAgg('day', distinct=True),
            # Get a single, representative ID for the entire group for booking purposes
            id=Min('id'),
        )

        return grouped_queryset.order_by('start_date', 'time')


# ============================================================================
# Student Course Enrollments (auth required)
# ============================================================================


class StudentCourseEnrollmentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for students to manage their course enrollments.

    Endpoints:
    - GET /api/student/course-enrollments/ - List my enrollments
    - GET /api/student/course-enrollments/{id}/ - Get enrollment details
    - POST /api/student/course-enrollments/ - Enroll in course
    - POST /api/student/course-enrollments/{id}/cancel/ - Cancel enrollment
    - POST /api/student/course-enrollments/{id}/drop/ - Drop mid-course
    """

    serializer_class = CourseEnrollmentSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        """Get enrollments for current user"""
        return (
            CourseEnrollment.objects.filter(user=self.request.user)
            .select_related("schedule__option__classId__businessId")
            .prefetch_related("all_session_bookings__schedule_instance")
            .order_by("-enrollment_date")
        )

    def get_serializer_class(self):
        """Use detail serializer for retrieve action"""
        if self.action == "retrieve":
            return CourseEnrollmentDetailSerializer
        elif self.action == "create":
            return CourseBookingCreateSerializer
        return super().get_serializer_class()

    def create(self, request, *args, **kwargs):
        """
        Enroll student in a course.
        Creates CourseEnrollment and all session Bookings atomically.
        """
        serializer = CourseBookingCreateSerializer(
            data=request.data, context={"request": request}
        )

        try:
            with transaction.atomic():
                serializer.is_valid(raise_exception=True)
                
                # 'validated_schedule' is the representative schedule from the group
                representative_schedule = serializer.context["validated_schedule"]

                # Find all sibling schedules in the multi-day group
                group_schedules = Schedule.objects.filter(
                    option=representative_schedule.option,
                    start_date=representative_schedule.start_date,
                    end_date=representative_schedule.end_date,
                    time=representative_schedule.time,
                    price=representative_schedule.price
                )

                # Collect all instances from all schedules in the group
                all_instances_in_group = []
                for schedule in group_schedules:
                    # Perform capacity check for each schedule in the group
                    # Aggregate total participants from course enrollments
                    current_enrolled = schedule.course_enrollments.filter(
                        status__in=['active', 'pending']
                    ).aggregate(total=Coalesce(Sum('participants'), 0))['total']
                    
                    available_spots = schedule.maxParticipants - current_enrolled
                    if available_spots < serializer.validated_data["participants"]:
                         raise ValidationError(f"Not enough spots available for the session on {schedule.day}.")
                    
                    # Add scheduled instances
                    all_instances_in_group.extend(list(schedule.instances.filter(status='scheduled').order_by('date')))
                
                # Sort all collected instances by date to ensure correct session numbering
                all_instances_in_group.sort(key=lambda x: x.date)

                if not all_instances_in_group:
                    raise ValidationError("This course has no upcoming sessions to book.")

                booking_group_id = uuid.uuid4()
                option = representative_schedule.option
                participants = serializer.validated_data["participants"]
                
                # Create a SINGLE CourseEnrollment record
                enrollment = CourseEnrollment.objects.create(
                    # NOTE: We link it to the representative schedule, but it covers the whole group
                    schedule=representative_schedule, 
                    user=request.user,
                    booking_group_id=booking_group_id,
                    total_sessions=len(all_instances_in_group),
                    participants=participants,
                    status="pending",
                    total_amount_paid=Decimal("0.00"),
                    cancellation_policy=option.cancellationPolicy,
                    cancellation_custom_hours=option.cancellationCustomHours,
                    cancellation_refund_percentage=option.cancellationRefundPercentage,
                )

                participant_details = serializer.validated_data.get("participant_details", [])
                notes = serializer.validated_data.get("notes", "")

                bookings = []
                for session_num, instance in enumerate(all_instances_in_group, start=1):
                    bookings.append(Booking(
                        user=request.user,
                        schedule_instance=instance,
                        enrollment_type="Full Course",
                        booking_group_id=booking_group_id,
                        course_session_number=session_num,
                        participants=participants,
                        participant_details=participant_details,
                        notes=notes,
                        amount_paid=Decimal("0.00"),
                        status="pending",
                        payment_status="pending",
                        cancellation_policy=enrollment.cancellation_policy,
                        cancellation_custom_hours=enrollment.cancellation_custom_hours,
                        cancellation_refund_percentage=enrollment.cancellation_refund_percentage,
                    ))

                created_bookings = Booking.objects.bulk_create(bookings)

                logger.info(
                    f"Created course enrollment {enrollment.id} with {len(created_bookings)} "
                    f"session bookings for user {request.user.email} covering {group_schedules.count()} weekly schedules."
                )

                response_data = {
                    "enrollment_id": str(enrollment.id),
                    "booking_group_id": str(booking_group_id),
                    "total_price": float(representative_schedule.price),
                    "total_sessions": len(all_instances_in_group),
                    "first_session_date": all_instances_in_group[0].date,
                    "last_session_date": all_instances_in_group[-1].date,
                    "requires_payment": True,
                }

                return Response(response_data, status=status.HTTP_201_CREATED)

        except ValidationError as e:
            logger.warning(f"Course enrollment validation failed: {e.detail}")
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"Unexpected error during course enrollment: {e}", exc_info=True
            )
            return Response(
                {"error": "An unexpected error occurred during enrollment."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel_enrollment(self, request, pk=None):
        """
        Cancel entire course enrollment.
        Only allowed before first session starts (subject to cancellation policy).
        """
        try:
            with transaction.atomic():
                enrollment = self.get_object()

                # Validation
                if enrollment.status not in ["pending", "active"]:
                    raise ValidationError(
                        f"Cannot cancel enrollment with status '{enrollment.status}'"
                    )

                # Get next session
                next_session = enrollment.next_session
                if not next_session:
                    raise ValidationError("No upcoming sessions to cancel")

                # Check cancellation policy
                self._validate_cancellation_policy(
                    next_session.schedule_instance,
                    enrollment.cancellation_policy,
                    enrollment.cancellation_custom_hours,
                )

                # Cancel enrollment
                enrollment.status = "cancelled"
                enrollment.cancelled_at = timezone.now()
                enrollment.cancellation_reason = "Cancelled by student"
                enrollment.save()

                # Cancel all future bookings
                future_bookings = Booking.objects.filter(
                    booking_group_id=enrollment.booking_group_id,
                    status__in=["pending", "confirmed"],
                    schedule_instance__date__gte=timezone.now().date(),
                )

                cancelled_count = future_bookings.update(
                    status="cancelled",
                    cancelled_at=timezone.now(),
                    cancellation_reason="Course cancelled by student",
                    payment_status="refund_pending",
                )

                logger.info(
                    f"Cancelled course enrollment {enrollment.id}, "
                    f"cancelled {cancelled_count} session bookings"
                )

                return Response(
                    {
                        "status": "cancelled",
                        "message": "Course enrollment cancelled successfully",
                        "refund_info": "A full refund will be processed if applicable",
                    },
                    status=status.HTTP_200_OK,
                )

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error cancelling course enrollment: {e}", exc_info=True)
            return Response(
                {"error": "An error occurred while cancelling the enrollment"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="drop")
    def drop_enrollment(self, request, pk=None):
        """
        Drop course mid-way through.
        Provides partial refund for remaining sessions.
        """
        try:
            with transaction.atomic():
                enrollment = self.get_object()

                if enrollment.status != "active":
                    raise ValidationError("Can only drop active course enrollments")

                # Get future sessions
                future_bookings = Booking.objects.filter(
                    booking_group_id=enrollment.booking_group_id,
                    status="confirmed",
                    schedule_instance__date__gte=timezone.now().date(),
                )

                if not future_bookings.exists():
                    raise ValidationError("No future sessions to drop")

                # Calculate refund (pro-rated)
                sessions_remaining = future_bookings.count()
                refund_percentage = Decimal(sessions_remaining) / Decimal(
                    enrollment.total_sessions
                )
                policy_percentage = Decimal(
                    enrollment.cancellation_refund_percentage
                ) / Decimal("100")
                refund_amount = (
                    enrollment.total_amount_paid * refund_percentage * policy_percentage
                )
                refund_amount = refund_amount.quantize(Decimal("0.01"))

                # Update enrollment
                enrollment.status = "dropped"
                enrollment.cancelled_at = timezone.now()
                enrollment.cancellation_reason = "Dropped by student mid-course"
                enrollment.save()

                # Cancel future bookings
                future_bookings.update(
                    status="cancelled",
                    cancelled_at=timezone.now(),
                    cancellation_reason="Course dropped by student",
                    payment_status="refund_pending",
                )

                logger.info(
                    f"Student dropped course enrollment {enrollment.id}, "
                    f"{sessions_remaining} sessions remaining, "
                    f"refund: ${refund_amount}"
                )

                return Response(
                    {
                        "status": "dropped",
                        "sessions_remaining": sessions_remaining,
                        "refund_amount": float(refund_amount),
                        "message": f"You will receive a ${refund_amount} refund for the remaining {sessions_remaining} sessions",
                    },
                    status=status.HTTP_200_OK,
                )

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Error dropping course enrollment: {e}", exc_info=True)
            return Response(
                {"error": "An error occurred while dropping the enrollment"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _validate_cancellation_policy(self, schedule_instance, policy, custom_hours):
        """Validate cancellation policy and raise error if not allowed"""
        from datetime import timedelta
        import pytz

        if policy == "strict":
            raise ValidationError(
                "This booking has a strict cancellation policy and cannot be cancelled."
            )

        business = schedule_instance.schedule.option.classId.businessId
        business_tz = pytz.timezone(business.business_timezone)

        instance_datetime = timezone.datetime.combine(
            schedule_instance.date, schedule_instance.time
        )
        instance_datetime_aware = business_tz.localize(instance_datetime)
        now_aware = timezone.now().astimezone(business_tz)

        if instance_datetime_aware <= now_aware:
            raise ValidationError(
                "Cannot cancel a class that has already started or passed."
            )

        if policy == "custom":
            required_hours = custom_hours or 0
        else:
            policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
            required_hours = policy_hours_map.get(policy, 0)

        time_until_class = instance_datetime_aware - now_aware
        hours_until_class = time_until_class.total_seconds() / 3600

        if hours_until_class < required_hours:
            raise ValidationError(
                f"Cancellation not allowed. This policy requires {required_hours} hours notice."
            )