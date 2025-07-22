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
    StudentBookingDetailSerializer,  # <-- IMPORT THE NEW DEDICATED SERIALIZER
    BookingDetailSerializer,  # This is for admin use, we don't use it here.
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
        This is a critical security and design step to prevent data leakage.
        """
        if self.action == "create":
            return BookingCreateSerializer
        if self.action == "retrieve":
            # Use the dedicated, curated serializer for student details.
            # This ensures no admin-level data is ever exposed to this endpoint.
            return StudentBookingDetailSerializer
        # Default is StudentBookingSerializer (for the list view)
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
        queryset = self.filter_queryset(queryset)  # Apply ordering

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
        # The queryset already ensures ownership, but an explicit check adds a layer of safety.
        if instance.user != request.user:
            raise PermissionDenied("You do not have permission to view this booking.")
        # get_serializer_class() now correctly returns StudentBookingDetailSerializer
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        """Creates one or more bookings based on selected slots for the current user."""
        if not request.user.has_perm("quickstart.add_booking"):
            raise PermissionDenied("You do not have permission to create bookings.")

        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        try:
            booking = serializer.save()
            logger.info(
                f"Booking creation initiated by user {request.user.email}. Initial Instance: {serializer.context['validated_instance'].pk}"
            )

            # Respond with the detailed student view of the newly created booking
            response_serializer = StudentBookingDetailSerializer(
                booking, context={"request": request}
            )
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            error_detail = e.detail if hasattr(e, "detail") else e.message_dict
            logger.warning(
                f"Booking creation failed validation for user {request.user.email}. Error: {error_detail}"
            )
            return Response(error_detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"Unexpected error creating booking for user {request.user.email}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected error occurred while creating the booking."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["get"], url_path="cancellation-info")
    def cancellation_info(self, request, pk=None):
        """
        Provides detailed, definitive information about the cancellation eligibility
        and policy for a specific booking. This is the single source of truth.

        MODIFIED: This now reads the policy directly from the Booking object itself,
        reflecting the terms at the time of purchase.
        """
        booking = self.get_object()
        if booking.status not in ["confirmed", "pending"]:
            return Response(
                {
                    "error": f"Cancellation info not applicable for a booking with status '{booking.status}'."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # --- MODIFIED: Policy details are now sourced directly from the booking ---
            policy_key = booking.cancellation_policy
            refund_percentage = booking.cancellation_refund_percentage

            # --- The rest of the logic uses the snapshotted policy ---
            schedule_instance = booking.schedule_instance
            if not schedule_instance:
                raise AttributeError("Booking is missing a schedule instance.")

            business = schedule_instance.schedule.option.classId.businessId
            if not business:
                raise AttributeError("Class is missing a business.")

            business_timezone_str = business.business_timezone
            if not business_timezone_str:
                raise ValueError("Business is missing a timezone setting.")

            business_tz = pytz.timezone(business_timezone_str)

            instance_datetime_naive = datetime.combine(
                schedule_instance.date, schedule_instance.time
            )
            instance_datetime_aware = business_tz.localize(instance_datetime_naive)
            now_aware_in_business_tz = timezone.now().astimezone(business_tz)

            can_cancel = instance_datetime_aware > now_aware_in_business_tz
            is_eligible_for_refund = False

            cancellation_deadline_aware = None
            cancellation_deadline_utc = None

            policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
            policy_description_map = {
                "flexible": f"Full refund if cancelled at least 1 hour before the class starts. A {refund_percentage}% refund applies.",
                "24h": f"A {refund_percentage}% refund applies if cancelled at least 24 hours before the class starts.",
                "48h": f"A {refund_percentage}% refund applies if cancelled at least 48 hours before the class starts.",
                "72h": f"A {refund_percentage}% refund applies if cancelled at least 72 hours before the class starts.",
                "strict": "This booking is non-refundable according to the strict policy.",
            }

            hours_notice_required = policy_hours_map.get(policy_key, 0)
            policy_description = policy_description_map.get(
                policy_key, "Standard cancellation policy applies."
            )

            if hours_notice_required > 0:
                cancellation_deadline_aware = instance_datetime_aware - timedelta(
                    hours=hours_notice_required
                )
                cancellation_deadline_utc = cancellation_deadline_aware.astimezone(
                    pytz.utc
                )

            if can_cancel and policy_key != "strict":
                if cancellation_deadline_aware:
                    if now_aware_in_business_tz <= cancellation_deadline_aware:
                        is_eligible_for_refund = True
                else:
                    is_eligible_for_refund = True

            return Response(
                {
                    "can_cancel": can_cancel,
                    "is_eligible_for_refund": is_eligible_for_refund,
                    "policy_key": policy_key,
                    "policy_description": policy_description,
                    "refund_percentage": refund_percentage,
                    "cancellation_deadline_utc": (
                        cancellation_deadline_utc.isoformat()
                        if cancellation_deadline_utc
                        else None
                    ),
                }
            )

        except (AttributeError, ValueError, pytz.UnknownTimeZoneError) as e:
            logger.error(
                f"Configuration or data integrity error for booking {pk}: {e}",
                exc_info=True,
            )
            return Response(
                {
                    "error": "Cannot retrieve cancellation policy due to incomplete or misconfigured booking/business data."
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as e:
            logger.error(
                f"Error generating cancellation info for booking {pk}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An internal server error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="cancel")
    def student_cancel(self, request, pk=None):
        """Allows a student to cancel their own booking, respecting policy."""
        booking = self.get_object()
        if booking.user != request.user:
            raise PermissionDenied("You cannot cancel this booking.")

        if not request.user.has_perm("quickstart.cancel_own_booking"):
            raise PermissionDenied("You do not have permission to cancel bookings.")

        if booking.status not in ["confirmed", "pending"]:
            raise ValidationError(
                {"status": f'Cannot cancel a booking with status "{booking.status}".'}
            )

        cancellation_reason = request.data.get("reason", "Cancelled by student.")
        needs_refund_processing = False  # Keep this here
        payment = None  # Keep this here

        if booking.status == "confirmed":
            try:
                schedule_instance = booking.schedule_instance
                class_option = schedule_instance.schedule.option
                policy = class_option.cancellationPolicy

                # 1. Immediately block "Strict" (non-cancellable) policies.
                if policy == "strict":
                    raise ValidationError(
                        {
                            "policy": "This booking has a strict policy and cannot be cancelled by a student."
                        }
                    )

                # 2. Get business timezone and create aware datetimes for accurate comparison.
                business_timezone_str = (
                    class_option.classId.businessId.business_timezone
                )
                business_tz = pytz.timezone(business_timezone_str)

                instance_datetime_naive = datetime.combine(
                    schedule_instance.date, schedule_instance.time
                )
                instance_datetime_aware = business_tz.localize(instance_datetime_naive)
                now_aware = timezone.now().astimezone(business_tz)

                # 3. Check if the class has already started.
                if instance_datetime_aware <= now_aware:
                    raise ValidationError(
                        {
                            "policy": "Cannot cancel a class that has already started or is in the past."
                        }
                    )

                # 4. Enforce time-based policies (24h, 48h, flexible, etc.).
                policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}
                required_hours = policy_hours_map.get(policy, 0)

                if required_hours > 0:
                    time_diff = instance_datetime_aware - now_aware
                    hours_until_class = time_diff.total_seconds() / 3600

                    if hours_until_class < required_hours:
                        raise ValidationError(
                            {
                                "policy": f"Cancellation not allowed. This policy requires {required_hours} hours notice."
                            }
                        )

                # 5. If all checks pass, the booking is cancellable and eligible for a refund.
                payment = (
                    booking.payments.filter(
                        status__in=["succeeded", "partially_refunded"]
                    )
                    .order_by("-created_at")
                    .first()
                )
                if payment:
                    needs_refund_processing = True

                # --- END: REWRITTEN VALIDATION LOGIC ---

            except pytz.UnknownTimeZoneError:
                logger.error(
                    f"Unknown timezone for business during cancellation check for booking {pk}."
                )
                raise ValidationError(
                    {"error": "System error: Could not verify business timezone."}
                )
            except ValidationError as ve:
                raise ve  # Re-raise policy validation errors to send 400 response
            except Exception as e:
                logger.error(
                    f"Unexpected error checking cancellation policy for booking {pk}: {e}",
                    exc_info=True,
                )
                raise ValidationError(
                    {"error": "An error occurred checking the cancellation policy."}
                )

        try:
            # This part of the logic for saving and sending emails remains the same.
            business_user_to_notify = (
                booking.schedule_instance.schedule.option.classId.businessId.owner
            )

            refund_details_message = "As per the cancellation policy, no refund was applicable for this cancellation."

            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = cancellation_reason

                if (
                    needs_refund_processing
                    and payment
                    and payment.available_refund_amount > 0
                ):
                    refund_amount_display = payment.available_refund_amount
                    # Re-assign the message if a refund is pending.
                    refund_details_message = (
                        f"A refund of ${refund_amount_display:.2f} will be processed."
                    )
                    booking.payment_status = "refund_pending"
                    logger.info(
                        f"Booking {pk} cancelled by student {request.user.email}. Marked for refund."
                    )
                else:
                    logger.info(
                        f"Booking {pk} cancelled by student {request.user.email}. No refund applicable."
                    )

                booking.save(
                    update_fields=[
                        "status",
                        "cancelled_at",
                        "cancellation_reason",
                        "payment_status",
                    ]
                )

            # Email notifications after successful transaction
            try:
                # Now, refund_details_message is guaranteed to have a value.
                send_booking_cancellation_user_email(
                    user=request.user,
                    booking=booking,
                    refund_details=refund_details_message,
                )
                if business_user_to_notify:
                    send_business_student_cancellation_email(
                        business_user=business_user_to_notify, booking=booking
                    )
            except Exception as email_error:
                logger.error(
                    f"Failed to send cancellation email for booking {pk}: {email_error}",
                    exc_info=True,
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
