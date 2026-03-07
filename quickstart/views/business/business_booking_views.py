from decimal import Decimal
from django.conf import settings
from django.db import models, transaction
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
    DateTimeField,
    Case,
    When,
    BooleanField,
    CharField,
    Min,
    Exists,
)
from django.db.models.functions import (
    TruncDate,
    ExtractWeekDay,
    datetime as ExtractDatetimeDb,  # Renamed to avoid conflict
    Concat,
    RowNumber,
    Cast,
    ExtractHour,
    Coalesce,
    TruncHour,  # Added TruncHour
)
from django.utils import timezone
from datetime import datetime, timedelta, date as datetime_date
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError
import pytz

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound
from rest_framework.pagination import PageNumberPagination

from quickstart.models import (
    BusinessInfo,
    Booking,
    ScheduleInstance,
    CustomUser,
    ClassesMain,
    Payment,
)
from quickstart.serializers.business.business_booking_serializers import (
    BusinessBookingListSerializer,
    BusinessBookingDetailSerializer,
)
from quickstart.utils.permissions import (
    IsAuthenticated,
    CanViewOwnBusinessBookings,
    CanManageOwnBusinessBookings,
    CanManageOwnClasses,
)
from quickstart.utils.email_utils import (
    send_booking_cancelled_by_other_email,
    send_booking_rescheduled_by_business_email,
)
from quickstart.utils.sms_utils import normalize_phone_for_sns, business_sms_enabled
from quickstart.tasks.notification_tasks import send_sms_task
import logging

logger = logging.getLogger(__name__)


class BusinessBookingPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


class BusinessBookingViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessBookingListSerializer
    permission_classes = [IsAuthenticated, CanViewOwnBusinessBookings]
    pagination_class = BusinessBookingPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "user__email",
        "user__first_name",
        "user__last_name",
        "contact__email",  # Added contact fields
        "contact__first_name",  # Added contact fields
        "contact__last_name",  # Added contact fields
        "schedule_instance__schedule__option__classId__title",
        "schedule_instance__schedule__option__classId__title",  # Corrected from option.title
        "id",
        "user_facing_reference",
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
            return BusinessBookingDetailSerializer
        return BusinessBookingListSerializer

    def get_business_context(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                raise PermissionDenied(
                    "You are not associated with an active business."
                )
            return business
        except BusinessInfo.DoesNotExist:
            raise PermissionDenied("You are not associated with a business.")

    def get_queryset(self):
        """
        This view returns a list of all bookings for the business...
        """
        user = self.request.user

        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()

        if not business:
            # ... (existing logging code)
            return Booking.objects.none()

        # Base queryset
        queryset = (
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business
            )
            .select_related(
                "user",
                "schedule_instance__schedule__option__classId",
                "contact",
            )
            .order_by("-booking_date")
        )

        time_threshold = timezone.now() - timedelta(minutes=20)
        queryset = queryset.exclude(
            status='pending', 
            booking_date__lt=time_threshold
        )

        # Apply status and date filters from the request query parameters.
        return self._apply_business_filters(queryset, self.request)

    def _apply_business_filters(self, queryset, request):
        status_param = request.query_params.get("status")
        if status_param and status_param != "all":
            status_list = [
                s.strip().lower() for s in status_param.split(",") if s.strip()
            ]
            if status_list:
                queryset = queryset.filter(status__in=status_list)

        start_date_str = request.query_params.get("start_date")
        end_date_str = request.query_params.get("end_date")
        if start_date_str:
            try:
                start_date_filter = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                queryset = queryset.filter(
                    schedule_instance__date__gte=start_date_filter
                )
            except ValueError:
                logger.warning(
                    f"Invalid start_date format for list filter: {start_date_str}"
                )
        if end_date_str:
            try:
                end_date_filter = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                queryset = queryset.filter(schedule_instance__date__lte=end_date_filter)
            except ValueError:
                logger.warning(
                    f"Invalid end_date format for list filter: {end_date_str}"
                )

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
            summary_qs = queryset
            response.data["summary"] = {
                "total_confirmed": summary_qs.filter(status="confirmed").count(),
                "total_completed": summary_qs.filter(status="completed").count(),
                "total_cancelled": summary_qs.filter(status="cancelled").count(),
                "total_bookings_in_filter": summary_qs.count(),
                "total_participant_spots_in_filter": summary_qs.aggregate(
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

    def _get_policy_check_details(self, booking):
        """
        Helper function to check if a reschedule is within the booking's policy.
        Returns a dictionary with the check result and a descriptive message.
        """
        policy_hours_map = {"flexible": 1, "24h": 24, "48h": 48, "72h": 72}

        # Use the policy snapshotted on the booking itself
        policy = booking.cancellation_policy

        if policy == "strict":
            return {
                "is_within_policy": False,
                "message": "The booking is under a strict (non-refundable/non-changeable) policy.",
            }

        # Get the original instance datetime
        original_instance = booking.schedule_instance
        business_tz_str = (
            original_instance.schedule.option.classId.businessId.business_timezone
        )
        business_tz = pytz.timezone(business_tz_str)
        instance_datetime_naive = datetime.combine(
            original_instance.date, original_instance.time
        )
        instance_datetime_aware = business_tz.localize(instance_datetime_naive)

        # Calculate notice period in hours
        notice_hours = 0
        if policy == "custom":
            notice_hours = booking.cancellation_custom_hours or 0
        else:
            notice_hours = policy_hours_map.get(policy, 0)

        if notice_hours == 0:
            return {
                "is_within_policy": True,
                "message": "The policy does not restrict this change.",
            }

        # Calculate the cutoff time
        cutoff_datetime = instance_datetime_aware - timedelta(hours=notice_hours)

        # Check if the current time is before the cutoff
        is_within_policy = timezone.now() < cutoff_datetime

        message = (
            f"The student is within the {notice_hours}-hour notice period."
            if is_within_policy
            else f"The student is outside the {notice_hours}-hour notice period."
        )

        return {"is_within_policy": is_within_policy, "message": message}

    @action(
        detail=True,
        methods=["get"],
        url_path="available-slots",
        permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings],
    )
    def available_slots(self, request, pk=None):
        """
        Returns a list of future, scheduled instances for a booking's class option,
        annotated with their validity for rescheduling.
        """
        booking = self.get_object()
        now = timezone.now()
        today = now.date()

        queryset = (
            ScheduleInstance.objects.filter(
                schedule__option=booking.schedule_instance.schedule.option,
                status="scheduled",
                date__gte=today,
            )
            .exclude(Q(date=today) & Q(time__lt=now.time()))
            .exclude(pk=booking.schedule_instance.pk)
            .annotate(
                current_occupancy=Coalesce(
                    Sum(
                        "bookings__participants",
                        filter=Q(bookings__status__in=["confirmed", "pending"]),
                    ),
                    0,
                )
            )
            .annotate(
                has_capacity=Case(
                    When(
                        max_participants__gte=F("current_occupancy")
                        + booking.participants,
                        then=True,
                    ),
                    default=False,
                    output_field=BooleanField(),
                )
            )
            .filter(has_capacity=True)  # FIX: Add this filter
            .values(
                "id",
                "date",
                "time",
                "price",
                "max_participants",
                "current_occupancy",
                "has_capacity",
            )
            .order_by("date", "time")
        )

        # Format the data for the frontend
        slots = []
        for inst in queryset:
            is_valid = inst["has_capacity"]  # For now, only capacity is a hard blocker
            reason_invalid = (
                ""
                if is_valid
                else f"Not enough spots. Only {inst['max_participants'] - inst['current_occupancy']} available."
            )

            slots.append(
                {
                    "id": inst["id"],
                    "date": inst["date"],
                    "time": inst["time"],
                    "price": inst["price"],
                    "available_spots": inst["max_participants"]
                    - inst["current_occupancy"],
                    "is_valid": is_valid,
                    "reason_invalid": reason_invalid,
                }
            )

        return Response(slots)

    @action(
        detail=True,
        methods=["post"],
        url_path="reschedule",
        permission_classes=[IsAuthenticated, CanManageOwnBusinessBookings],
    )
    def reschedule(self, request, pk=None):
        booking = self.get_object()
        new_instance_id = request.data.get("new_schedule_instance_id")
        dry_run = request.data.get("dry_run", False)

        if not new_instance_id:
            raise ValidationError(
                {"new_schedule_instance_id": "This field is required."}
            )

        # --- Initial Validations ---
        if booking.status not in ["confirmed"]:
            raise ValidationError(
                {
                    "status": f"Cannot reschedule a booking with status '{booking.status}'."
                }
            )

        try:
            new_instance = ScheduleInstance.objects.get(id=new_instance_id)
        except ScheduleInstance.DoesNotExist:
            raise NotFound("The selected new session could not be found.")

        # Create a timezone-aware datetime for the new instance's start time
        business_tz_str = (
            new_instance.schedule.option.classId.businessId.business_timezone
        )
        try:
            business_tz = pytz.timezone(business_tz_str)
        except pytz.UnknownTimeZoneError:
            business_tz = pytz.utc  # Fallback to UTC
            logger.warning(f"Invalid business timezone '{business_tz_str}'. Using UTC.")

        new_instance_datetime_naive = datetime.combine(
            new_instance.date, new_instance.time
        )
        new_instance_datetime_aware = business_tz.localize(new_instance_datetime_naive)

        # Compare with the current time
        if new_instance_datetime_aware < timezone.now():
            raise ValidationError(
                {
                    "new_schedule_instance_id": "Cannot reschedule to a session in the past."
                }
            )

        if new_instance.available_spots < booking.participants:
            raise ValidationError(
                {
                    "new_schedule_instance_id": "The selected new session does not have enough available spots."
                }
            )

        # --- Perform Policy and Price Check ---
        policy_details = self._get_policy_check_details(booking)
        price_difference = new_instance.price - booking.schedule_instance.price

        # --- Handle Dry Run Request ---
        if dry_run:
            return Response(
                {
                    "status": "check_success",
                    "is_within_policy": policy_details["is_within_policy"],
                    "policy_message": policy_details["message"],
                    "warning_required": not policy_details["is_within_policy"],
                    "price_difference": price_difference,
                    "original_price": booking.schedule_instance.price,
                    "new_price": new_instance.price,
                },
                status=status.HTTP_200_OK,
            )

        # --- Handle Execution Request ---
        original_instance = booking.schedule_instance
        # The 'reason' is not used by the email function, but we keep it here in case it's needed elsewhere
        reschedule_reason = request.data.get("reason", "Operational change.")

        try:
            with transaction.atomic():
                booking.schedule_instance = new_instance
                booking.is_rescheduled = True
                booking.original_schedule_instance = original_instance
                booking.rescheduled_at = timezone.now()
                booking.rescheduled_by = request.user

                # IMPORTANT: We DO NOT change booking.amount_paid. It remains as the historical record of the transaction.
                booking.save(
                    update_fields=[
                        "schedule_instance",
                        "is_rescheduled",
                        "original_schedule_instance",
                        "rescheduled_at",
                        "rescheduled_by",
                    ]
                )

                logger.info(
                    f"Booking {booking.id} rescheduled from instance {original_instance.id} to {new_instance.id} by user {request.user.email}."
                )

                send_booking_rescheduled_by_business_email(
                    user=(booking.user or booking.contact),
                    booking=booking,
                    old_instance=original_instance,
                    new_instance=new_instance,
                )

            serializer = self.get_serializer(booking)
            return Response(serializer.data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(
                f"Error during reschedule for booking {pk}: {e}", exc_info=True
            )
            return Response(
                {"error": "An error occurred while rescheduling the booking."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

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

        instance_datetime = datetime.combine(
            booking.schedule_instance.date, booking.schedule_instance.time
        )
        business_tz_str = (
            booking.schedule_instance.schedule.option.classId.businessId.business_timezone
        )
        try:
            business_tz = pytz.timezone(business_tz_str)
        except pytz.UnknownTimeZoneError:
            business_tz = pytz.utc
            logger.warning(f"Unknown business timezone '{business_tz_str}'. Using UTC.")
        aware_instance_datetime = business_tz.localize(instance_datetime)

        if aware_instance_datetime < timezone.now():
            raise ValidationError({"date": "Cannot cancel bookings for past sessions."})

        user_to_notify = booking.user or booking.contact
        try:
            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = (
                    f"Cancelled by business: {cancellation_reason}"
                )
                if booking.payment_status == "paid":
                    booking.payment_status = "refund_pending"
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
                except Exception as email_error:
                    logger.error(
                        f"Failed to send cancellation email for booking {booking.id}: {email_error}",
                        exc_info=True,
                    )
                business = booking.schedule_instance.schedule.option.classId.businessId
                if business_sms_enabled(business) and user_to_notify and booking.schedule_instance:
                    phone = getattr(user_to_notify, "phone_number", None) or (booking.metadata or {}).get("guest_phone") or ""
                    normalized = normalize_phone_for_sns(phone)
                    if normalized:
                        class_title = getattr(booking.schedule_instance.schedule.option.classId, "title", "Class")
                        date_str = booking.schedule_instance.date.strftime("%b %d")
                        t = booking.schedule_instance.time
                        time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                        business_name = getattr(business, "businessName", "") or "ClassEasily"
                        sms_msg = f"Your booking for {class_title} on {date_str} at {time_str} has been cancelled.\n\nIf you paid, you'll receive a refund.\n\n— {business_name}"
                        try:
                            send_sms_task.delay(normalized, sms_msg)
                        except Exception as sms_e:
                            logger.warning("Cancellation SMS failed for booking %s: %s", booking.id, sms_e)
            serializer = BusinessBookingDetailSerializer(
                booking, context={"request": request}
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        except ValidationError as ve:
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
        business_pytz = pytz.timezone(business.business_timezone)

        try:
            start_datetime_utc, end_datetime_utc = self._get_date_range_for_analytics(
                request
            )

            bookings_qs_base = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__range=[start_datetime_utc, end_datetime_utc],
            )

            class_id_filter = request.query_params.get("class_id")
            if class_id_filter and class_id_filter.isdigit():
                bookings_qs_base = bookings_qs_base.filter(
                    schedule_instance__schedule__option__classId_id=class_id_filter
                )

            source_filter = request.query_params.get("source", "all")
            if source_filter not in ("widget", "marketplace", "all"):
                source_filter = "all"
            _widget_payment_exists = Payment.objects.filter(
                booking=OuterRef("pk"),
                status="succeeded",
                metadata__original_stripe_metadata__booking_source="widget",
            )
            if source_filter == "widget":
                bookings_qs_base = bookings_qs_base.filter(Exists(_widget_payment_exists))
            elif source_filter == "marketplace":
                bookings_qs_base = bookings_qs_base.exclude(Exists(_widget_payment_exists))

            bookings_qs = bookings_qs_base.select_related(
                "schedule_instance__schedule__option__classId",
                "user",
                "contact",
            )

            # --- Summary Aggregates (Simplified) ---
            total_aggregates = bookings_qs.aggregate(
                total_booking_transactions=Count("id"),
                total_participant_spots=Coalesce(Sum("participants"), Value(0)),
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

            user_bookings_in_business = (
                bookings_qs.annotate(
                    booker_email=Coalesce(
                        F("user__email"),
                        F("contact__email"),
                        output_field=CharField(),
                    )
                )
                .values("booker_email")
                .annotate(booking_tx_count=Count("id"))
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

            # --- Daily Trends (Remains the same) ---
            daily_trends_data = (
                bookings_qs.annotate(
                    date_local=TruncDate(F("booking_date"), tzinfo=business_pytz)
                )
                .values("date_local")
                .annotate(
                    new_booking_transactions=Count("id"),
                    new_participant_spots=Coalesce(Sum("participants"), Value(0)),
                    cancelled_booking_transactions=Count(
                        "id", filter=Q(status="cancelled")
                    ),
                    cancelled_participant_spots=Coalesce(
                        Sum("participants", filter=Q(status="cancelled")), Value(0)
                    ),
                )
                .order_by("date_local")
            )

            all_trend_dates_local = {}  # Keyed by local date string
            current_scan_local_date = start_datetime_utc.astimezone(
                business_pytz
            ).date()
            end_scan_local_date = end_datetime_utc.astimezone(business_pytz).date()

            while current_scan_local_date <= end_scan_local_date:
                date_iso = current_scan_local_date.isoformat()
                all_trend_dates_local[date_iso] = {
                    "date": date_iso,  # Local date
                    "new_booking_transactions": 0,
                    "new_participant_spots": 0,
                    "cancelled_booking_transactions": 0,
                    "cancelled_participant_spots": 0,
                    "net_participant_spots": 0,
                    "cancellation_rate_by_transaction": 0.0,  # Renamed to be specific
                }
                current_scan_local_date += timedelta(days=1)

            for trend in daily_trends_data:
                local_date_iso = trend["date_local"].isoformat()
                if local_date_iso in all_trend_dates_local:
                    all_trend_dates_local[local_date_iso][
                        "new_booking_transactions"
                    ] = trend["new_booking_transactions"]
                    all_trend_dates_local[local_date_iso]["new_participant_spots"] = (
                        trend["new_participant_spots"]
                    )
                    all_trend_dates_local[local_date_iso][
                        "cancelled_booking_transactions"
                    ] = trend["cancelled_booking_transactions"]
                    all_trend_dates_local[local_date_iso][
                        "cancelled_participant_spots"
                    ] = trend["cancelled_participant_spots"]
                    all_trend_dates_local[local_date_iso]["net_participant_spots"] = (
                        trend["new_participant_spots"]
                        - trend["cancelled_participant_spots"]
                    )
                    all_trend_dates_local[local_date_iso][
                        "cancellation_rate_by_transaction"
                    ] = round(
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
                    )
            processed_trends = sorted(
                all_trend_dates_local.values(), key=lambda x: x["date"]
            )

            # --- Popular Classes (Added Revenue) ---
            popular_classes_data = (
                bookings_qs.values(
                    "schedule_instance__schedule__option__classId__title",
                    "schedule_instance__schedule__option__classId_id",
                )
                .annotate(
                    total_booking_transactions=Count("id"),
                    total_participant_spots=Coalesce(Sum("participants"), Value(0)),
                    unique_bookers=Count(
                        Coalesce(F("user__email"), F("contact__email")), distinct=True
                    ),
                    class_cancelled_spots=Coalesce(
                        Sum("participants", filter=Q(status="cancelled")), Value(0)
                    ),
                    total_revenue_for_class=Coalesce(
                        Sum("amount_paid", filter=Q(payment_status="paid")),
                        Value(Decimal("0.0")),
                    ),
                )
                .order_by("-total_participant_spots")
            )
            popular_classes = [
                {
                    "class_id": str(entry["schedule_instance__schedule__option__classId_id"]),
                    "class_name": entry[
                        "schedule_instance__schedule__option__classId__title"
                    ],
                    "total_booking_transactions": entry["total_booking_transactions"],
                    "total_participant_spots": entry["total_participant_spots"],
                    "unique_bookers": entry["unique_bookers"],
                    "total_revenue": float(entry["total_revenue_for_class"]),
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

            # --- Time Distribution (based on LOCAL business hour of booking) ---
            time_dist_data = (
                bookings_qs.annotate(
                    local_booking_hour_group=TruncHour(
                        F("booking_date"), tzinfo=business_pytz
                    )
                )
                .values(
                    hour_local=ExtractHour(
                        "local_booking_hour_group"
                    )  # Extract hour from the localized timestamp
                )
                .annotate(
                    booking_transactions=Count("id"),
                    participant_spots=Coalesce(Sum("participants"), Value(0)),
                    cancelled_transactions=Count("id", filter=Q(status="cancelled")),
                )
                .order_by("hour_local")
            )

            time_distribution_local = [
                {
                    "hour": h,
                    "booking_transactions": 0,
                    "participant_spots": 0,
                    "cancelled_transactions": 0,
                }
                for h in range(24)
            ]
            for entry in time_dist_data:
                hour_idx = entry["hour_local"]
                if 0 <= hour_idx < 24:
                    time_distribution_local[hour_idx]["booking_transactions"] = entry[
                        "booking_transactions"
                    ]
                    time_distribution_local[hour_idx]["participant_spots"] = entry[
                        "participant_spots"
                    ]
                    time_distribution_local[hour_idx]["cancelled_transactions"] = entry[
                        "cancelled_transactions"
                    ]

            # --- Booking Type Distribution (as before) ---
            type_dist_data = (
                bookings_qs.values("enrollment_type")
                .annotate(
                    transaction_count=Count("id"),
                    spot_count=Coalesce(Sum("participants"), Value(0)),
                )
                .order_by("-spot_count")
            )
            total_spots_for_types = total_aggregates["total_participant_spots"]
            booking_types = [
                {
                    "type": entry["enrollment_type"],
                    "transaction_count": entry["transaction_count"],
                    "spot_count": entry["spot_count"],
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

            # --- Booking Lead Time Analysis (Simplified: avg lead time) ---
            avg_lead_time_data = (
                bookings_qs.filter(status__in=["confirmed", "completed"])
                .annotate(
                    lead_interval=ExpressionWrapper(
                        F("schedule_instance__date") - TruncDate(F("booking_date")),
                        output_field=models.DurationField(),
                    )
                )
                .aggregate(avg_lead_time=Avg("lead_interval"))
            )
            avg_lead_time_days = (
                avg_lead_time_data["avg_lead_time"].days
                if avg_lead_time_data["avg_lead_time"]
                else 0
            )

            # --- CORRECTED: New vs. Returning Student Bookings ---
            # Step 1: Identify all unique bookers (by user_id or contact_id) in the period
            bookers_in_period_qs = (
                bookings_qs_base.annotate(
                    booker_user_id=F("user_id"), booker_contact_id=F("contact_id")
                )
                .values("booker_user_id", "booker_contact_id")
                .distinct()
            )

            # Step 2: For each booker, find their first-ever booking date for this business
            first_booking_subquery = (
                Booking.objects.filter(
                    schedule_instance__schedule__option__classId__businessId=business
                )
                .filter(
                    # Match on user OR contact, handling nulls
                    (Q(user_id=OuterRef("booker_user_id")) & Q(user_id__isnull=False))
                    | (
                        Q(contact_id=OuterRef("booker_contact_id"))
                        & Q(contact_id__isnull=False)
                    )
                )
                .order_by("booking_date")
                .values("booking_date")[:1]
            )

            # Step 3: Annotate the bookers with their first booking date
            bookers_with_first_date = bookers_in_period_qs.annotate(
                first_booking_date=Subquery(
                    first_booking_subquery, output_field=DateTimeField()
                )
            )

            # Step 4: Categorize bookers into new vs. returning
            new_booker_filter = Q(
                first_booking_date__range=(start_datetime_utc, end_datetime_utc)
            )
            returning_booker_filter = Q(first_booking_date__lt=start_datetime_utc)

            new_bookers = bookers_with_first_date.filter(new_booker_filter)
            returning_bookers = bookers_with_first_date.filter(returning_booker_filter)

            # Step 5: Construct filters to count bookings from each group
            new_booker_q = Q()
            for b in new_bookers:
                if b["booker_user_id"]:
                    new_booker_q |= Q(user_id=b["booker_user_id"])
                if b["booker_contact_id"]:
                    new_booker_q |= Q(contact_id=b["booker_contact_id"])

            returning_booker_q = Q()
            for b in returning_bookers:
                if b["booker_user_id"]:
                    returning_booker_q |= Q(user_id=b["booker_user_id"])
                if b["booker_contact_id"]:
                    returning_booker_q |= Q(contact_id=b["booker_contact_id"])

            new_student_bookings = (
                bookings_qs_base.filter(new_booker_q).count() if new_booker_q else 0
            )
            returning_student_bookings = (
                bookings_qs_base.filter(returning_booker_q).count()
                if returning_booker_q
                else 0
            )

            # --- Occupancy Rate Trends ---
            avg_occupancy_data = bookings_qs.filter(
                schedule_instance__max_participants__gt=0,  # Avoid division by zero
                status__in=["confirmed", "completed"],
            ).aggregate(
                avg_occupancy_percentage=Avg(
                    ExpressionWrapper(
                        100.0
                        * F("participants")
                        / F("schedule_instance__max_participants"),
                        output_field=FloatField(),
                    )
                )
            )
            average_occupancy_rate = (
                avg_occupancy_data["avg_occupancy_percentage"] or 0.0
            )

            # --- NEW: Upcoming Classes (Next 7 days) ---
            today_local = timezone.now().astimezone(business_pytz).date()
            seven_days_later = today_local + timedelta(days=7)

            upcoming_instances_qs = (
                ScheduleInstance.objects.filter(
                    schedule__option__classId__businessId=business,
                    date__gte=today_local,
                    date__lt=seven_days_later,
                    status="scheduled",
                )
                .select_related("schedule__option__classId")
                .annotate(
                    current_occupancy=Coalesce(
                        Sum(
                            "bookings__participants",
                            filter=Q(bookings__status="confirmed"),
                        ),
                        0,
                        output_field=IntegerField(),
                    )
                )
                .order_by("date", "time")[:5]
            )

            upcoming_classes_data = []
            for inst in upcoming_instances_qs:
                start_time_obj = inst.time
                end_time_obj = (
                    datetime.combine(datetime_date.min, start_time_obj)
                    + timedelta(minutes=inst.duration)
                ).time()
                time_str = f"{start_time_obj.strftime('%I:%M %p').lstrip('0')} - {end_time_obj.strftime('%I:%M %p').lstrip('0')}"
                date_str = inst.date.strftime("%a, %b %d")

                upcoming_classes_data.append(
                    {
                        "schedule_instance_id": inst.id,
                        "class_id": inst.schedule.option.classId.classId,
                        "name": inst.schedule.option.classId.title,
                        "time": f"{date_str}, {time_str}",
                        "current_occupancy": inst.current_occupancy,
                        "max_occupancy": inst.max_participants,
                    }
                )

            response_data = {
                "business_id": business.businessId,
                "business_name": business.businessName,
                "date_range": {
                    "start": start_datetime_utc.date().isoformat(),
                    "end": end_datetime_utc.date().isoformat(),
                },
                "summary": {
                    "total_booking_transactions": total_booking_transactions,
                    "total_participant_spots": total_aggregates[
                        "total_participant_spots"
                    ],
                    "active_booking_transactions": total_aggregates[
                        "confirmed_transactions"
                    ],
                    "completed_booking_transactions": total_aggregates[
                        "completed_transactions"
                    ],
                    "cancelled_booking_transactions": cancelled_transactions,
                    "total_revenue": float(total_aggregates["total_revenue"]),
                    "cancellation_rate_by_transaction": round(cancellation_rate, 1),
                    "booker_retention_rate": round(booker_retention_rate, 1),
                    "average_lead_time_days": avg_lead_time_days,
                    "new_student_bookings": new_student_bookings,
                    "returning_student_bookings": returning_student_bookings,
                    "average_occupancy_rate": round(average_occupancy_rate, 1),
                },
                "trends": processed_trends,
                "class_insights": {"popular_classes": popular_classes},
                "booking_patterns": {
                    "time_distribution": time_distribution_local,
                    "booking_types": booking_types,
                },
                "upcoming_classes": upcoming_classes_data,
            }
            return Response(response_data)

        except ValidationError as e:
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

    def _get_date_range_for_analytics(self, request):
        try:
            start_date_str = request.query_params.get("start_date")
            end_date_str = request.query_params.get("end_date")
            today_utc_date = timezone.now().date()
            if start_date_str:
                start_date_naive = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            else:
                start_date_naive = today_utc_date - timedelta(days=29)
            if end_date_str:
                end_date_naive = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            else:
                end_date_naive = today_utc_date
            if start_date_naive > end_date_naive:
                raise ValidationError("Start date cannot be after end date.")
            start_datetime_utc = timezone.make_aware(
                datetime.combine(start_date_naive, datetime.min.time()), pytz.utc
            )
            end_datetime_utc = timezone.make_aware(
                datetime.combine(end_date_naive, datetime.max.time()), pytz.utc
            )
            return start_datetime_utc, end_datetime_utc
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
