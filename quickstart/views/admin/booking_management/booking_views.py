# quickstart/views/admin/booking_management/booking_views.py

from decimal import Decimal
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db import transaction
from django.db.models import (
    Q,
    Sum,
    Count,
    DecimalField,
    IntegerField,
    Prefetch,
    F,
    Value,
    ExpressionWrapper,
)
from django.db.models.functions import Coalesce
from rest_framework.pagination import PageNumberPagination
import logging
import csv
from django.http import HttpResponse
import stripe
from django.conf import settings

from quickstart.models import AuditLog, Booking, ScheduleInstance, Payment, CustomUser

from quickstart.serializers.admin.booking_management.payment_serializers import (
    AdminBookingListSerializer,
    AdminBookingPaymentSerializer,
)
from quickstart.serializers import BookingDetailSerializer
from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessBookingAdmin,
    CanManageTargetBooking,
)

try:
    from quickstart.utils.email_utils import send_booking_cancelled_by_other_email
except ImportError:
    logging.error("Could not import email utility functions in booking_views.py")

    # Define dummy functions if needed to prevent NameErrors during development
    def send_booking_cancelled_by_other_email(*args, **kwargs):
        logging.warning("Dummy send_booking_cancelled_by_other_email called.")
        pass

from quickstart.utils.sms_utils import normalize_phone_for_sns, business_sms_enabled
from quickstart.tasks.notification_tasks import send_sms_task
from quickstart.views.admin.metrics_time_windows import get_admin_metrics_window


logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminBookingViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for managing bookings"""

    permission_classes = [IsAuthenticated, CanAccessBookingAdmin]
    pagination_class = StandardResultsSetPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "user_facing_reference",
        "user__first_name",
        "user__last_name",
        "user__email",
        "schedule_instance__schedule__option__classId__title",
        "schedule_instance__schedule__option__classId__businessId__businessName",
        "id",
    ]
    ordering_fields = [
        "booking_date",
        "status",
        "payment_status",
        "amount_paid",
        "schedule_instance__date",
    ]
    ordering = ["-booking_date"]

    def get_serializer_class(self):
        if self.action == "list":
            return AdminBookingListSerializer
        # For retrieve, update, etc., use the comprehensive admin detail serializer
        return BookingDetailSerializer

    def get_queryset(self):
        if not self.request.user.has_perm("quickstart.view_booking"):
            logger.warning(
                f"User {self.request.user.email} denied access to list bookings (missing view_booking perm)."
            )
            return Booking.objects.none()

        # --- FIX: Optimized the queryset for performance ---
        queryset = (
            Booking.objects.select_related(
                # Use select_related for all forward foreign key relationships.
                # This turns many small queries into a single, larger, more efficient JOIN query.
                "schedule_instance__schedule__option__classId__businessId",
                "user__role",  # Also join user and their role
                "contact",  # Also join contact for guest bookings
            )
            .prefetch_related(
                # Use prefetch_related for reverse relationships (like payments).
                # This performs a separate lookup for all payments needed for the initial bookings,
                # avoiding one query per booking.
                Prefetch("payments", queryset=Payment.objects.order_by("-created_at"))
            )
            .distinct()
        )
        # Filtering logic
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        payment_status_filter = self.request.query_params.get("payment_status")
        if payment_status_filter:
            queryset = queryset.filter(payment_status=payment_status_filter)
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date and end_date:
            try:
                start_dt = timezone.datetime.strptime(start_date, "%Y-%m-%d").date()
                end_dt = timezone.datetime.strptime(end_date, "%Y-%m-%d").date()
                queryset = queryset.filter(
                    schedule_instance__date__range=[start_dt, end_dt]
                )
            except ValueError:
                logger.warning(
                    f"Invalid date format for booking filter: start={start_date}, end={end_date}"
                )

        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """
        Retrieves a single booking's details for an admin.
        Crucially, it also finds the associated payment and attaches it to the response.
        """
        if not request.user.has_perm("quickstart.view_booking"):
            self.permission_denied(request, message="You cannot view booking details.")

        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data

        # Find the primary payment and serialize it for the frontend
        payment = instance.payments.order_by("-created_at").first()
        if payment:
            data["payment"] = AdminBookingPaymentSerializer(payment).data
        else:
            data["payment"] = None

        return Response(data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessBookingAdmin],
    )
    def cancel(self, request, pk=None):
        """Admin cancel booking action (Updates booking status & sends email)"""
        # 1. Check for the specific permission required for this action.
        if not request.user.has_perm("quickstart.cancel_any_booking"):
            self.permission_denied(
                request, message="You do not have permission to cancel this booking."
            )

        booking = self.get_object()

        # 2. Check the booking's current status.
        if booking.status not in ["confirmed", "pending"]:
            return Response(
                {
                    "error": f'Booking with status "{booking.status}" cannot be cancelled.'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = request.data.get("reason", "Cancelled by administrator")

        try:
            with transaction.atomic():
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = reason

                update_fields = ["status", "cancelled_at", "cancellation_reason"]

                # If the booking was paid, flag it for refund processing.
                if booking.payment_status == "paid":
                    booking.payment_status = "refund_pending"
                    update_fields.append("payment_status")

                booking.save(update_fields=update_fields)

                # Send email notification after successful save.
                try:
                    send_booking_cancelled_by_other_email(
                        user=booking.user,
                        booking=booking,
                        cancelled_by="an administrator",
                        reason=reason,
                        contact_info=settings.NOTIFICATION_SETTINGS.get(
                            "reply_to", "support@classeasily.com"
                        ),
                    )
                    logger.info(
                        f"Cancellation email prepared for user {booking.user.email} for booking {booking.id}"
                    )
                except Exception as email_error:
                    logger.error(
                        f"Failed to send cancellation email for booking {booking.id}: {email_error}",
                        exc_info=True,
                    )
                try:
                    business = booking.schedule_instance.schedule.option.classId.businessId
                    if business_sms_enabled(business) and booking.schedule_instance:
                        user_to_notify = booking.user or booking.contact
                        if user_to_notify:
                            phone = getattr(user_to_notify, "phone_number", None) or (booking.metadata or {}).get("guest_phone") or ""
                            normalized = normalize_phone_for_sns(phone)
                            if normalized:
                                class_title = getattr(booking.schedule_instance.schedule.option.classId, "title", "Class")
                                date_str = booking.schedule_instance.date.strftime("%b %d")
                                t = booking.schedule_instance.time
                                time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                                business_name = getattr(business, "businessName", "") or "ClassEasily"
                                sms_msg = f"Your booking for {class_title} on {date_str} at {time_str} has been cancelled.\n\nIf you paid, you'll receive a refund.\n\n— {business_name}"
                                send_sms_task.delay(normalized, sms_msg)
                except Exception as sms_e:
                    logger.warning("Cancellation SMS failed for booking %s: %s", booking.id, sms_e)

            log_details = f"Booking cancelled by admin {request.user.email}. Reason: {reason}. Refund must be processed separately if applicable."
            self._log_booking_action(
                booking, "booking_cancel_admin", log_details, request
            )

        except Exception as e:
            logger.error(f"Error cancelling Booking {pk}: {str(e)}", exc_info=True)
            return Response(
                {"error": f"Failed to cancel booking. {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Return the updated booking details with payment info attached
        return self.retrieve(request, pk=pk)

    def partial_update(self, request, *args, **kwargs):
        # This is kept minimal, for things like adding notes.
        # Status changes should go through dedicated actions.
        if not request.user.has_perm("quickstart.change_booking"):
            self.permission_denied(
                request, message="You do not have permission to update bookings."
            )
        instance = self.get_object()
        if not user_can_manage(request.user, instance.user):
            self.permission_denied(
                request,
                message="You cannot manage this booking due to hierarchy restrictions.",
            )

        # Only allow updating specific, non-critical fields
        allowed_fields = ["notes"]
        update_data = {k: v for k, v in request.data.items() if k in allowed_fields}

        if not update_data:
            return Response(
                {"detail": "No valid fields provided for update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(instance, data=update_data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        self._log_booking_action(
            instance,
            "booking_update_admin",
            f"Booking updated. Changes: {update_data}",
            request,
        )
        return Response(serializer.data)

    def _log_booking_action(self, booking, action_code, details, request):
        try:
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action_code,
                details=details,
                target_user=booking.user,
                target_model="Booking",
                target_id=str(booking.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                metadata={"status": booking.status},
            )
        except Exception as e:
            logger.error(
                f"Failed to create audit log for booking action {action_code}: {str(e)}",
                exc_info=True,
            )

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_booking"):
            self.permission_denied(
                request, message="You do not have permission to delete bookings."
            )
        instance = self.get_object()
        if not user_can_manage(request.user, instance.user):
            self.permission_denied(
                request,
                message="You cannot delete this booking due to hierarchy restrictions.",
            )
        logger.warning(
            f"Booking ID {instance.pk} deleted by Admin {request.user.email}"
        )
        # Add to AuditLog if needed
        self._log_booking_action(
            instance,
            "booking_delete_admin",
            f"Booking deleted by admin {request.user.email}",
            request,
        )
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """
        Provides operational statistics for the Booking Management dashboard.

        Date ranges use the class session date (schedule_instance.date), matching
        get_queryset() for the list and export — not booking_date (when the row was created).
        """
        if not request.user.has_perm("quickstart.view_booking_analytics"):
            self.permission_denied(
                request, message="You cannot view booking analytics."
            )

        try:
            window = get_admin_metrics_window(
                request.query_params, default_days=30, logger=logger
            )

            # Align with list/export: filter by session date when a range is implied; all rows for all_time.
            if window.all_time:
                bookings_in_period = Booking.objects.all()
            else:
                bookings_in_period = Booking.objects.filter(
                    schedule_instance__date__gte=window.start_date,
                    schedule_instance__date__lte=window.end_date,
                )

            aggregates = bookings_in_period.aggregate(
                total_bookings=Count("id"),
                confirmed_bookings=Count("id", filter=Q(status="confirmed")),
                completed_bookings=Count("id", filter=Q(status="completed")),
                cancelled_bookings=Count("id", filter=Q(status="cancelled")),
                total_participants=Coalesce(
                    Sum(
                        "participants", filter=Q(status__in=["confirmed", "completed"])
                    ),
                    0,
                    output_field=IntegerField(),
                ),
                _total_revenue_for_avg=Coalesce(
                    Sum("amount_paid", filter=Q(status__in=["confirmed", "completed"])),
                    Decimal(0),
                    output_field=DecimalField(),
                ),
            )

            total_bookings = aggregates["total_bookings"]
            confirmed_and_completed_count = (
                aggregates["confirmed_bookings"] + aggregates["completed_bookings"]
            )
            cancellation_rate = (
                (aggregates["cancelled_bookings"] / total_bookings * 100)
                if total_bookings > 0
                else 0
            )
            avg_booking_value = (
                (aggregates["_total_revenue_for_avg"] / confirmed_and_completed_count)
                if confirmed_and_completed_count > 0
                else Decimal(0)
            )
            avg_participants_per_booking = (
                (aggregates["total_participants"] / confirmed_and_completed_count)
                if confirmed_and_completed_count > 0
                else 0
            )

            if window.all_time:
                booking_growth = 0.0
            else:
                previous_period_bookings_count = Booking.objects.filter(
                    schedule_instance__date__gte=window.previous_start_date,
                    schedule_instance__date__lte=window.previous_end_date,
                ).count()

                booking_growth = 0
                if previous_period_bookings_count > 0:
                    booking_growth = (
                        (total_bookings - previous_period_bookings_count)
                        / previous_period_bookings_count
                    ) * 100

            total_confirmed_revenue = float(aggregates["_total_revenue_for_avg"])

            # Platform and Stripe fees from payments for these bookings (succeeded only). Stripe = 2.9% + $0.30 per payment (on the fly).
            payments_for_bookings = Payment.objects.filter(
                booking__status__in=["confirmed", "completed"],
                status="succeeded",
            )
            if not window.all_time:
                payments_for_bookings = payments_for_bookings.filter(
                    booking__schedule_instance__date__gte=window.start_date,
                    booking__schedule_instance__date__lte=window.end_date,
                )
            stripe_fee_expr = ExpressionWrapper(
                F("amount") * Decimal("0.029") + Value(Decimal("0.30")),
                output_field=DecimalField(),
            )
            fee_agg = payments_for_bookings.annotate(
                _stripe_fee=stripe_fee_expr
            ).aggregate(
                platform_fees=Coalesce(Sum("platform_fee_amount"), Decimal(0)),
                stripe_fees=Coalesce(Sum("_stripe_fee"), Decimal(0)),
            )
            total_platform_fees = float(fee_agg["platform_fees"])
            total_stripe_fees = float(fee_agg["stripe_fees"])

            response_data = {
                "total_bookings": total_bookings,
                "confirmed_bookings": aggregates["confirmed_bookings"],
                "completed_bookings": aggregates["completed_bookings"],
                "cancelled_bookings": aggregates["cancelled_bookings"],
                "total_participants": aggregates["total_participants"],
                "cancellation_rate": round(cancellation_rate, 1),
                "average_booking_value": float(avg_booking_value),
                "average_participants_per_booking": round(
                    avg_participants_per_booking, 2
                ),
                "booking_growth": round(booking_growth, 1),
                "total_confirmed_revenue": total_confirmed_revenue,
                "total_platform_fees": total_platform_fees,
                "total_stripe_fees": total_stripe_fees,
                "start_date": window.start_date.strftime("%Y-%m-%d"),
                "end_date": window.end_date.strftime("%Y-%m-%d"),
                "all_time": window.all_time,
            }

            return Response(response_data)
        except Exception as e:
            logger.error(
                f"Critical error in booking analytics: {str(e)}", exc_info=True
            )
            return Response(
                {"error": "Failed to retrieve booking analytics"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"])
    def export(self, request):
        if not request.user.has_perm("quickstart.export_booking_data"):
            self.permission_denied(request, message="You cannot export booking data.")
        try:
            queryset = self.filter_queryset(self.get_queryset())
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = (
                'attachment; filename="bookings_export.csv"'
            )
            writer = csv.writer(response)
            writer.writerow(
                [
                    "Booking ID",
                    "Student Name",
                    "Student Email",
                    "Class",
                    "Business",
                    "Session Date",
                    "Session Time",
                    "Status",
                    "Payment Status",
                    "Amount Paid",
                    "Participants",
                    "Booking Date",
                    "Payment Intent ID",
                ]
            )
            for booking in queryset.iterator():
                payment_id = (
                    booking.payments.first().stripe_payment_intent_id
                    if booking.payments.exists()
                    else ""
                )
                writer.writerow(
                    [
                        booking.id,
                        f"{booking.user.first_name} {booking.user.last_name}".strip(),
                        booking.user.email,
                        (
                            booking.schedule_instance.schedule.option.classId.title
                            if booking.schedule_instance
                            else "N/A"
                        ),
                        (
                            booking.schedule_instance.schedule.option.classId.businessId.businessName
                            if booking.schedule_instance
                            else "N/A"
                        ),
                        (
                            booking.schedule_instance.date
                            if booking.schedule_instance
                            else "N/A"
                        ),
                        (
                            booking.schedule_instance.time
                            if booking.schedule_instance
                            else "N/A"
                        ),
                        booking.status,
                        booking.payment_status,
                        booking.amount_paid,
                        booking.participants,
                        booking.booking_date.strftime("%Y-%m-%d %H:%M:%S"),
                        payment_id,
                    ]
                )
            return response
        except Exception as e:
            logger.error(f"Error exporting bookings: {str(e)}", exc_info=True)
            return HttpResponse(
                f"Error exporting booking data: {str(e)}",
                status=500,
                content_type="text/plain",
            )
