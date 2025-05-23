# views/business/business_booking_views.py
from decimal import Decimal
from django.conf import settings
from django.db import models, transaction  # Added models
from django.db.models import (
    Q,
    Sum,
    Count,
    Avg,
    F,
    Prefetch,
    Window,
    Value,
    FloatField,
    ExpressionWrapper,
    Subquery,
    OuterRef,
    IntegerField,
)  # Added Value
from django.db.models.functions import (
    TruncDate,
    ExtractWeekDay,
    datetime,
    Concat,
    RowNumber,
    Cast,
    ExtractHour,
    Coalesce,
)  # Added datetime
from django.utils import timezone
from datetime import (
    datetime,
    timedelta,
)  # Ensure datetime is imported from datetime module
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import (
    ValidationError,
    PermissionDenied,
    NotFound,
)  # Added NotFound
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.pagination import PageNumberPagination

# Adjust import paths as needed
from ...models import BusinessInfo, Booking, ScheduleInstance, CustomUser
from ...serializers import (
    BookingDetailSerializer,  # Generic detail
    BusinessBookingListSerializer,  # Specific list for business
)

# Import specific business permissions
from ...utils.permissions import (
    CanManageOwnClasses,
)  # For checking if user can manage related class

import logging

logger = logging.getLogger(__name__)

from ...utils.email_utils import send_booking_cancelled_by_other_email


# --- Permissions ---
class CanViewOwnBusinessBookings(BasePermission):
    message = "You do not have permission to view bookings for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        has_base_perm = user.has_perm("quickstart.view_own_business_bookings")
        has_business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).exists()
        return has_base_perm and has_business

    def has_object_permission(self, request, view, obj):
        user = request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return False
        try:
            return obj.schedule_instance.schedule.option.classId.businessId == business
        except AttributeError:
            return False


class CanManageOwnBusinessBookings(BasePermission):
    message = "You do not have permission to manage this booking."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            user.has_perm("quickstart.view_own_business_bookings")
            and (
                user.has_perm("quickstart.cancel_business_booking")
                or user.has_perm("quickstart.mark_booking_attendance")
            )
            and BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
        )

    def has_object_permission(self, request, view, obj):
        user = request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return False
        try:
            return obj.schedule_instance.schedule.option.classId.businessId == business
        except AttributeError:
            return False


# --- Pagination ---
class BusinessBookingPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


# --- Business ViewSet ---
class BusinessBookingViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessBookingListSerializer
    permission_classes = [IsAuthenticated, CanViewOwnBusinessBookings]
    pagination_class = BusinessBookingPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "user__email",
        "user__first_name",
        "user__last_name",
        "schedule_instance__schedule__option__classId__title",
        "schedule_instance__schedule__option__title",
        "id",
    ]
    ordering_fields = [
        "schedule_instance__date",
        "booking_date",
        "status",
        "user__first_name",
        "user__last_name",
        "schedule_instance__schedule__option__classId__title",
    ]
    ordering = ["-schedule_instance__date", "-schedule_instance__time"]
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return BookingDetailSerializer
        return BusinessBookingListSerializer

    def get_business_context(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.get(Q(owner=user) | Q(managers=user))
            return business
        except BusinessInfo.DoesNotExist:
            raise PermissionDenied("You are not associated with a business.")
        except BusinessInfo.MultipleObjectsReturned:
            logger.error(
                f"User {user.email} is owner/manager of multiple businesses. Ambiguous context."
            )
            raise PermissionDenied(
                "Ambiguous business context. Please contact support."
            )

    def get_queryset(self):
        business = self.get_business_context()
        queryset = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business
        ).select_related(
            "schedule_instance__schedule__option__classId",
            "schedule_instance__schedule__option",
            "user",
        )
        queryset = self._apply_business_filters(queryset, self.request)
        return queryset.distinct()

    def _apply_business_filters(self, queryset, request):
        status_param = request.query_params.get("status")
        if status_param and status_param != "all":
            status_list = [s.strip() for s in status_param.split(",") if s.strip()]
            if status_list:
                queryset = queryset.filter(status__in=status_list)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if start_date:
            queryset = queryset.filter(schedule_instance__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(schedule_instance__date__lte=end_date)

        class_id = request.query_params.get("class_id")
        if class_id and class_id.isdigit():
            queryset = queryset.filter(
                schedule_instance__schedule__option__classId_id=class_id
            )

        user_id = request.query_params.get("user_id")
        if user_id and user_id.isdigit():
            queryset = queryset.filter(user_id=user_id)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, many=True, context={"request": request}
            )
            response = self.get_paginated_response(serializer.data)
            business = self.get_business_context()
            # Apply same filters to count_qs for accurate summary
            count_qs_base = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business
            )
            count_qs_filtered = self._apply_business_filters(count_qs_base, request)

            response.data["summary"] = {
                "total_confirmed": count_qs_filtered.filter(status="confirmed").count(),
                "total_completed": count_qs_filtered.filter(status="completed").count(),
                "total_cancelled": count_qs_filtered.filter(status="cancelled").count(),
                "total_bookings": count_qs_filtered.count(),  # Total booking transactions matching filters
                # Total participant spots matching filters
                "total_participant_spots": count_qs_filtered.aggregate(
                    total_spots=Coalesce(Sum("participants"), Value(0))
                )["total_spots"],
            }
            return response

        serializer = self.get_serializer(
            queryset, many=True, context={"request": request}
        )
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings],
    )
    def mark_attendance(self, request, pk=None):
        if not request.user.has_perm("quickstart.mark_booking_attendance"):
            raise PermissionDenied("You do not have permission to mark attendance.")
        booking = self.get_object()
        attended_status = request.data.get("attended")
        if attended_status is None or not isinstance(attended_status, bool):
            raise ValidationError(
                {"attended": 'Boolean field "attended" (true/false) is required.'}
            )
        if booking.status != "confirmed":
            raise ValidationError(
                {"status": "Can only mark attendance for confirmed bookings."}
            )

        with transaction.atomic():
            booking.attendance_marked = True
            booking.attended = attended_status
            if attended_status and booking.status == "confirmed":
                booking.status = "completed"
            booking.save(update_fields=["attendance_marked", "attended", "status"])
            instance = booking.schedule_instance
            if not instance.bookings.filter(
                attendance_marked=False, status="confirmed"
            ).exists():
                instance.attendance_marked = True
                instance.save(update_fields=["attendance_marked"])
        logger.info(
            f"Attendance marked for Booking {pk} (Attended: {attended_status}) by business user {request.user.email}"
        )
        serializer = BookingDetailSerializer(booking, context={"request": request})
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        url_path="cancel",
        permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings],
    )
    def business_cancel(self, request, pk=None):
        if not request.user.has_perm("quickstart.cancel_business_booking"):
            raise PermissionDenied(
                "You do not have permission to cancel bookings for this business."
            )
        booking = self.get_object()
        cancellation_reason = request.data.get("reason", "").strip()
        if not cancellation_reason:
            raise ValidationError({"reason": "A reason is required for cancellation."})
        if booking.status not in ["confirmed", "pending"]:
            raise ValidationError(
                {"status": f'Cannot cancel a booking with status "{booking.status}".'}
            )
        user_to_notify = booking.user
        try:
            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = (
                    f"Cancelled by business: {cancellation_reason}"
                )
                if booking.payment_status == "paid":
                    booking.payment_status = "refund_pending"
                    logger.info(
                        f"Booking {pk} cancelled by business user {request.user.email}. Marked payment as refund_pending."
                    )
                else:
                    logger.info(
                        f"Booking {pk} cancelled by business user {request.user.email}. No refund processing needed (Payment Status: {booking.payment_status})."
                    )
                booking.save(
                    update_fields=[
                        "status",
                        "cancelled_at",
                        "cancellation_reason",
                        "payment_status",
                    ]
                )
                try:
                    contact_info = settings.NOTIFICATION_SETTINGS.get(
                        "reply_to", "support@classeasily.com"
                    )
                    send_booking_cancelled_by_other_email(
                        user=user_to_notify,
                        booking=booking,
                        cancelled_by="the business",
                        reason=cancellation_reason,
                        contact_info=contact_info,
                    )
                    logger.info(
                        f"'Cancelled by other' email prepared/queued for user {user_to_notify.email} for booking {booking.id}"
                    )
                except Exception as email_error:
                    logger.error(
                        f"Failed to send cancellation email for booking {booking.id}: {email_error}",
                        exc_info=True,
                    )
            serializer = BookingDetailSerializer(booking, context={"request": request})
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as ve:
            logger.warning(
                f"Validation error during business cancel for booking {pk}: {ve.detail}"
            )
            return Response(ve.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"Error during business cancellation for booking {pk}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while cancelling the booking."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated])
    def analytics(self, request):
        if not request.user.has_perm("quickstart.view_own_booking_analytics"):
            raise PermissionDenied(
                "You do not have permission to view booking analytics for this business."
            )
        business = self.get_business_context()
        try:
            start_datetime, end_datetime = self._get_date_range(request)
            bookings_qs = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__range=[start_datetime, end_datetime],
            ).select_related(
                "schedule_instance__schedule__option__classId",
                "schedule_instance__schedule__option",
                "user",
            )

            total_aggregates = bookings_qs.aggregate(
                total_booking_transactions=Count("id"),  # Number of booking actions
                total_participant_spots=Coalesce(
                    Sum("participants"), Value(0)
                ),  # Total spots booked
                confirmed_transactions=Count("id", filter=Q(status="confirmed")),
                completed_transactions=Count("id", filter=Q(status="completed")),
                cancelled_transactions=Count("id", filter=Q(status="cancelled")),
                total_revenue=Coalesce(
                    Sum(
                        "amount_paid",
                        filter=Q(
                            status__in=["completed", "confirmed"], payment_status="paid"
                        ),
                    ),
                    Value(Decimal("0.0")),
                    output_field=models.DecimalField(),
                ),
            )
            total_booking_transactions = total_aggregates["total_booking_transactions"]
            cancelled_transactions = total_aggregates["cancelled_transactions"]
            cancellation_rate = (
                (cancelled_transactions / total_booking_transactions * 100)
                if total_booking_transactions > 0
                else 0
            )

            completed_qs = bookings_qs.filter(status="completed")
            total_completed_for_attendance = completed_qs.aggregate(
                total_spots=Coalesce(Sum("participants"), Value(0))
            )["total_spots"]
            attended_spots = completed_qs.filter(
                attendance_marked=True, attended=True
            ).aggregate(total_spots=Coalesce(Sum("participants"), Value(0)))[
                "total_spots"
            ]
            attendance_rate = (
                (attended_spots / total_completed_for_attendance * 100)
                if total_completed_for_attendance > 0
                else 0
            )

            user_bookings_in_business = bookings_qs.values("user").annotate(
                booking_tx_count=Count("id")
            )
            total_unique_bookers = user_bookings_in_business.count()
            repeat_bookers = user_bookings_in_business.filter(
                booking_tx_count__gt=1
            ).count()
            booker_retention_rate = (
                (repeat_bookers / total_unique_bookers * 100)
                if total_unique_bookers > 0
                else 0
            )

            daily_trends_data = (
                bookings_qs.annotate(date=TruncDate("booking_date"))
                .values("date")
                .annotate(
                    new_booking_transactions=Count("id"),
                    new_participant_spots=Sum("participants"),
                    cancelled_booking_transactions=Count(
                        "id", filter=Q(status="cancelled")
                    ),
                )
                .order_by("date")
            )

            processed_trends = [
                {
                    "date": trend["date"].isoformat(),
                    "new_booking_transactions": trend["new_booking_transactions"],
                    "new_participant_spots": trend["new_participant_spots"] or 0,
                    "cancellation_rate": round(
                        (
                            (
                                trend["cancelled_booking_transactions"]
                                / trend["new_booking_transactions"]
                                * 100
                            )
                            if trend["new_booking_transactions"] > 0
                            else 0
                        ),
                        1,
                    ),
                }
                for trend in daily_trends_data
            ]

            popular_classes_data = (
                bookings_qs.values(
                    "schedule_instance__schedule__option__classId__title"
                )
                .annotate(
                    total_booking_transactions=Count("id"),
                    total_participant_spots=Sum(
                        "participants"
                    ),  # Sum of participants for this class
                    unique_bookers=Count("user", distinct=True),
                    class_completed_spots=Sum(
                        "participants", filter=Q(status="completed")
                    ),
                    class_attended_spots=Sum(
                        "participants",
                        filter=Q(
                            status="completed", attendance_marked=True, attended=True
                        ),
                    ),
                    class_cancelled_spots=Sum(
                        "participants", filter=Q(status="cancelled")
                    ),
                )
                .order_by("-total_participant_spots")
            )  # Order by total spots

            popular_classes = [
                {
                    "class_name": entry[
                        "schedule_instance__schedule__option__classId__title"
                    ],
                    "total_booking_transactions": entry["total_booking_transactions"],
                    "total_participant_spots": entry["total_participant_spots"] or 0,
                    "unique_bookers": entry["unique_bookers"],
                    "attendance_rate": round(
                        (
                            (
                                entry["class_attended_spots"]
                                / entry["class_completed_spots"]
                                * 100
                            )
                            if entry["class_completed_spots"]
                            else 0
                        ),
                        1,
                    ),
                    "cancellation_rate_by_spots": round(
                        (
                            (
                                entry["class_cancelled_spots"]
                                / entry["total_participant_spots"]
                                * 100
                            )
                            if entry["total_participant_spots"]
                            else 0
                        ),
                        1,
                    ),
                }
                for entry in popular_classes_data
                if entry["schedule_instance__schedule__option__classId__title"]
            ]

            time_dist_data = (
                bookings_qs.annotate(
                    hour=ExtractHour(
                        "booking_date"
                    )  # Assuming booking_date is what matters for "time of day" pattern
                )
                .values("hour")
                .annotate(
                    booking_transactions=Count("id"),
                    participant_spots=Sum("participants"),
                    cancelled_transactions=Count("id", filter=Q(status="cancelled")),
                )
                .order_by("hour")
            )
            time_distribution = [
                {
                    "hour": entry["hour"],
                    "booking_transactions": entry["booking_transactions"],
                    "participant_spots": entry["participant_spots"] or 0,
                    "cancelled_transactions": entry["cancelled_transactions"],
                }
                for entry in time_dist_data
            ]

            type_dist_data = (
                bookings_qs.values("enrollment_type")
                .annotate(transaction_count=Count("id"), spot_count=Sum("participants"))
                .order_by("-spot_count")
            )

            total_spots_for_types = total_aggregates["total_participant_spots"]
            booking_types = [
                {
                    "type": entry["enrollment_type"],
                    "transaction_count": entry["transaction_count"],
                    "spot_count": entry["spot_count"] or 0,
                    "percentage_of_spots": round(
                        (
                            (entry["spot_count"] / total_spots_for_types * 100)
                            if total_spots_for_types > 0
                            else 0
                        ),
                        1,
                    ),
                }
                for entry in type_dist_data
                if entry["enrollment_type"]
            ]

            response_data = {
                "business_id": business.businessId,
                "business_name": business.businessName,
                "date_range": {
                    "start": start_datetime.date().isoformat(),
                    "end": end_datetime.date().isoformat(),
                },
                "summary": {
                    "total_booking_transactions": total_booking_transactions,
                    "total_participant_spots": total_aggregates[
                        "total_participant_spots"
                    ],
                    "active_booking_transactions": total_aggregates[
                        "confirmed_transactions"
                    ],  # Renamed for clarity
                    "completed_booking_transactions": total_aggregates[
                        "completed_transactions"
                    ],  # Renamed for clarity
                    "cancelled_booking_transactions": cancelled_transactions,  # Renamed for clarity
                    "total_revenue": float(total_aggregates["total_revenue"]),
                    "cancellation_rate_by_transaction": round(cancellation_rate, 1),
                    "attendance_rate_by_spot": round(attendance_rate, 1),
                    "booker_retention_rate": round(booker_retention_rate, 1),
                },
                "trends": processed_trends,
                "class_insights": {"popular_classes": popular_classes},
                "booking_patterns": {
                    "time_distribution": time_distribution,
                    "booking_types": booking_types,
                },
            }
            return Response(response_data)
        except ValidationError as e:
            logger.warning(
                f"Validation error in booking analytics for business {business.businessId}: {e.detail}"
            )
            return Response({"error": e.detail}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(
                f"Error in booking analytics for business {business.businessId}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while generating analytics."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _get_date_range(self, request):
        try:
            start_date_str = request.query_params.get("start_date")
            end_date_str = request.query_params.get("end_date")
            today = timezone.now().date()  # tz-aware now(), then .date()
            if start_date_str:
                start_date = timezone.datetime.strptime(
                    start_date_str, "%Y-%m-%d"
                ).date()
            else:
                start_date = today - timedelta(days=30)
            if end_date_str:
                end_date = timezone.datetime.strptime(end_date_str, "%Y-%m-%d").date()
            else:
                end_date = today
            if start_date > end_date:
                raise ValidationError("Start date cannot be after end date.")
            start_datetime = timezone.make_aware(
                datetime.combine(start_date, datetime.min.time()), timezone.utc
            )
            end_datetime = timezone.make_aware(
                datetime.combine(end_date, datetime.max.time()), timezone.utc
            )
            return start_datetime, end_datetime
        except ValueError:
            raise ValidationError("Invalid date format. Use YYYY-MM-DD")

    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
