from django.db import transaction
from django.utils import timezone
from datetime import datetime, timedelta
from django.shortcuts import get_object_or_404
from django.db.models import Prefetch
import pytz
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

# Adjust import paths as needed
from quickstart.models import Booking, ScheduleInstance, Reviews, ClassImage
from quickstart.serializers import (
    BookingCreateSerializer,
    StudentBookingSerializer,
    StudentBookingDetailSerializer,
)

from quickstart.utils.email_utils import (
    send_booking_cancellation_user_email,
    send_business_student_cancellation_email,
)

import logging

logger = logging.getLogger(__name__)


# --- Pagination ---
class StudentBookingPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


# --- Student ViewSet ---
class StudentBookingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Students to manage their own Bookings.
    Handles listing own bookings, retrieving details, creating, and cancelling.
    """

    serializer_class = StudentBookingSerializer  # Default for list
    permission_classes = [IsAuthenticated]
    pagination_class = StudentBookingPagination
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["schedule_instance__date", "booking_date", "status"]
    ordering = ["-schedule_instance__date", "-schedule_instance__time"]

    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        """
        Override to provide the correct serializer based on the action.
        """
        if self.action == "create":
            return BookingCreateSerializer
        if self.action == "retrieve":
            return StudentBookingDetailSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        """Filters queryset to only bookings belonging to the current authenticated user."""
        user = self.request.user
        if not user or not user.is_authenticated:
            return Booking.objects.none()

        return (
            Booking.objects.filter(user=user)
            .select_related(
                "schedule_instance__schedule__option__classId__businessId",
                "schedule_instance__schedule__option__classId",
                "schedule_instance__schedule__option",
                "review",
            )
            .prefetch_related(
                Prefetch(
                    "schedule_instance__schedule__option__classId__images",
                    queryset=ClassImage.objects.order_by("createdAt"),
                    to_attr="prefetched_images",
                )
            )
            .distinct()
        )

    def _apply_student_filters(self, queryset, request):
        status_param = request.query_params.get("status", None)
        if status_param and status_param != "all":
            queryset = queryset.filter(status=status_param)

        when = request.query_params.get("when", None)
        today = timezone.now().date()
        if when == "upcoming":
            queryset = queryset.filter(schedule_instance__date__gte=today)
        elif when == "past":
            queryset = queryset.filter(schedule_instance__date__lt=today)

        return queryset

    def list(self, request, *args, **kwargs):
        """Lists the current user's bookings."""
        queryset = self.get_queryset()
        queryset = self._apply_student_filters(queryset, request)
        queryset = self.filter_queryset(queryset)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, many=True, context={"request": request}
            )
            response = self.get_paginated_response(serializer.data)
            base_count_qs = Booking.objects.filter(user=request.user)
            response.data["summary"] = {
                "total_upcoming": base_count_qs.filter(
                    status="confirmed",
                    schedule_instance__date__gte=timezone.now().date(),
                ).count(),
                "total_completed": base_count_qs.filter(status="completed").count(),
                "total_cancelled": base_count_qs.filter(status="cancelled").count(),
            }
            return response

        serializer = self.get_serializer(
            queryset, many=True, context={"request": request}
        )
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """Retrieve details of a specific booking owned by the current user."""
        instance = self.get_object()
        if instance.user != request.user:
            raise PermissionDenied("You do not have permission to view this booking.")
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        """Creates one or more bookings for the current user."""
        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        try:
            # Wrap validation and creation in a single atomic transaction.
            # This ensures that select_for_update() in the serializer works correctly to prevent race conditions.
            with transaction.atomic():
                serializer.is_valid(raise_exception=True)

                # Now that validation (including the lock and availability check) has passed,
                # we can safely create the booking.
                validated_instance = serializer.context["validated_instance"]
                class_option = validated_instance.schedule.option

                # Snapshot the policy details at the time of creation
                snapshotted_policy = class_option.cancellationPolicy
                snapshotted_custom_hours = (
                    class_option.cancellationCustomHours
                    if snapshotted_policy == "custom"
                    else None
                )
                snapshotted_refund_percent = class_option.cancellationRefundPercentage

                booking = Booking.objects.create(
                    user=request.user,
                    schedule_instance=validated_instance,
                    participants=serializer.validated_data["participants"],
                    participant_details=serializer.validated_data.get(
                        "participant_details", []
                    ),
                    notes=serializer.validated_data.get("notes", ""),
                    amount_paid=0,
                    status="pending",
                    payment_status="pending",
                    cancellation_policy=snapshotted_policy,
                    cancellation_custom_hours=snapshotted_custom_hours,
                    cancellation_refund_percentage=snapshotted_refund_percent,
                )
                response_serializer = StudentBookingDetailSerializer(
                    booking, context={"request": request}
                )
                return Response(
                    response_serializer.data, status=status.HTTP_201_CREATED
                )
        except ValidationError as e:
            # Catch the validation error raised by is_valid()
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Unexpected error creating booking: {e}", exc_info=True)
            return Response(
                {"error": "An unexpected server error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["get"], url_path="cancellation-info")
    def cancellation_info(self, request, pk=None):
        """
        Provides details about the cancellation policy and eligibility for a specific booking.
        Uses the policy snapshotted at the time of booking.
        """
        booking = self.get_object()

        # --- Validation ---
        if booking.status != "confirmed":
            raise ValidationError(
                {
                    "detail": f'Cannot get cancellation info for a booking with status "{booking.status}".'
                }
            )

        try:
            # --- Get necessary objects and timezone ---
            schedule_instance = booking.schedule_instance
            business_tz_str = (
                schedule_instance.schedule.option.classId.businessId.business_timezone
            )
            business_tz = pytz.timezone(business_tz_str)

            # --- Localize instance start time and current time ---
            instance_datetime_local = datetime.combine(
                schedule_instance.date, schedule_instance.time
            )
            instance_datetime_aware = business_tz.localize(instance_datetime_local)
            now_aware = timezone.now().astimezone(business_tz)

            # --- Determine cancellability ---
            can_cancel = instance_datetime_aware > now_aware

            # --- Determine refund eligibility based on the SNAPSHOTTED policy ---
            is_eligible_for_refund = False
            policy_key = booking.cancellation_policy

            required_hours = 0
            if policy_key == "custom":
                required_hours = booking.cancellation_custom_hours or 0
            else:
                policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
                required_hours = policy_hours_map.get(policy_key, 0)

            deadline_aware = instance_datetime_aware - timedelta(hours=required_hours)

            if can_cancel and policy_key != "strict":
                if now_aware < deadline_aware:
                    is_eligible_for_refund = True

            # --- Generate human-readable policy description ---
            policy_descriptions = {
                "flexible": f"Full refund if you cancel at least 1 hour before the class starts.",
                "24h": "Full refund if you cancel at least 24 hours before the class starts.",
                "48h": "Full refund if you cancel at least 48 hours before the class starts.",
                "72h": "Full refund if you cancel at least 72 hours before the class starts.",
                "strict": "This booking is non-refundable and cannot be cancelled for a refund.",
                "custom": (
                    f"Full refund if you cancel at least {booking.cancellation_custom_hours} hours before the class starts."
                    if booking.cancellation_custom_hours
                    else "Custom cancellation policy applies."
                ),
            }
            policy_description = policy_descriptions.get(
                policy_key, "Standard cancellation policy applies."
            )

            # --- Construct response payload ---
            response_data = {
                "can_cancel": can_cancel,
                "is_eligible_for_refund": is_eligible_for_refund,
                "policy_key": policy_key,
                "policy_description": policy_description,
                "refund_percentage": (
                    booking.cancellation_refund_percentage
                    if is_eligible_for_refund
                    else 0
                ),
                "cancellation_deadline_utc": (
                    deadline_aware.astimezone(pytz.utc).isoformat()
                    if policy_key != "strict"
                    else None
                ),
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(
                f"Error fetching cancellation info for booking {pk}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while retrieving cancellation details."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="cancel")
    def student_cancel(self, request, pk=None):
        """
        Allows a student to cancel their own booking.
        This logic is now the definitive source of truth for cancellation eligibility.
        """
        booking = self.get_object()

        # --- Initial validation ---
        if booking.user != request.user:
            raise PermissionDenied("You cannot cancel this booking.")
        if booking.status != "confirmed":
            raise ValidationError(
                f'Cannot cancel a booking with status "{booking.status}".'
            )

        cancellation_reason = request.data.get("reason", "Cancelled by student.")
        needs_refund_processing = False

        try:
            # --- ROBUST CANCELLATION & REFUND ELIGIBILITY CHECK ---
            schedule_instance = booking.schedule_instance

            # 1. Get policy directly from the booking object (the "snapshot")
            policy = booking.cancellation_policy
            if policy == "strict":
                raise ValidationError(
                    {
                        "policy": "This booking has a strict policy and cannot be cancelled."
                    }
                )

            # 2. Prevent cancellations after the class has started (solves the race condition)
            business_tz = pytz.timezone(
                schedule_instance.schedule.option.classId.businessId.business_timezone
            )
            instance_datetime_aware = business_tz.localize(
                datetime.combine(schedule_instance.date, schedule_instance.time)
            )
            now_aware = timezone.now().astimezone(business_tz)

            if instance_datetime_aware <= now_aware:
                raise ValidationError(
                    {
                        "policy": "Cannot cancel a class that has already started or is in the past."
                    }
                )

            # 3. Enforce time-based policies (24h, 48h, etc.)
            required_hours = 0
            if policy == "custom":
                required_hours = booking.cancellation_custom_hours or 0
            else:
                policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
                required_hours = policy_hours_map.get(policy, 0)

            if required_hours > 0:
                time_until_class = instance_datetime_aware - now_aware
                if time_until_class.total_seconds() / 3600 < required_hours:
                    raise ValidationError(
                        {
                            "policy": f"Cancellation not allowed. This policy requires {required_hours} hours notice."
                        }
                    )

            # 4. If all checks pass, the booking is cancellable and eligible for refund processing.
            payment = booking.payments.filter(
                status__in=["succeeded", "partially_refunded"]
            ).first()
            if payment and payment.available_refund_amount > 0:
                needs_refund_processing = True

        except ValidationError as ve:
            raise ve
        except Exception as e:
            logger.error(
                f"Unexpected error checking cancellation policy for booking {pk}: {e}",
                exc_info=True,
            )
            raise ValidationError(
                {"error": "An error occurred while checking the cancellation policy."}
            )

        # --- DATABASE UPDATE ---
        try:
            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = cancellation_reason

                if needs_refund_processing:
                    booking.payment_status = "refund_pending"
                    logger.info(
                        f"Booking {pk} cancelled by student. Marked for automated refund."
                    )
                else:
                    logger.info(
                        f"Booking {pk} cancelled by student. No refund applicable."
                    )

                booking.save(
                    update_fields=[
                        "status",
                        "cancelled_at",
                        "cancellation_reason",
                        "payment_status",
                    ]
                )

            # Send email notifications after successful transaction
            business_user = (
                booking.schedule_instance.schedule.option.classId.businessId.owner
            )
            send_booking_cancellation_user_email(
                user=request.user,
                booking=booking,
                refund_details="A refund will be processed if applicable.",
            )
            send_business_student_cancellation_email(
                business_user=business_user, booking=booking
            )

            serializer = self.get_serializer(booking)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(
                f"Error during booking cancellation save/email for pk={pk}: {e}",
                exc_info=True,
            )
            return Response(
                {"detail": "An error occurred while cancelling the booking."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
