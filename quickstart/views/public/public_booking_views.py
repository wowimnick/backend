# views/public/public_booking_views.py
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
        serializer.is_valid(raise_exception=True)
        try:
            booking = serializer.save()
            response_serializer = StudentBookingDetailSerializer(
                booking, context={"request": request}
            )
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Unexpected error creating booking: {e}", exc_info=True)
            return Response(
                {"error": "An unexpected server error occurred."},
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
