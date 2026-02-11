from rest_framework import viewsets, status, filters  # Added filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from django.db import transaction
from django.db.models import Sum, Count, Avg, Q  # Added Q for search filter
from django.db.models.functions import Coalesce  # Added Coalesce
from datetime import timedelta
from decimal import Decimal

from quickstart.models import AuditLog, Payment
from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessPaymentAdmin,
    CanManageTargetPayment,
)
from quickstart.serializers.admin.booking_management.payment_serializers import (
    AdminPaymentSerializer,
)  # No need for AdminBookingPaymentSerializer here

import stripe
from django.conf import settings
import logging
import csv
from django.http import HttpResponse

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class AdminPaymentPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


# --- ViewSet ---


class AdminPaymentViewSet(viewsets.ModelViewSet):
    """Admin-only viewset for managing payments"""

    permission_classes = [IsAuthenticated, CanAccessPaymentAdmin]  # Base permission
    serializer_class = AdminPaymentSerializer
    pagination_class = AdminPaymentPagination
    # Allow GET, POST (for refund), PATCH (for mark_paid if needed), DELETE (if allowed)
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "stripe_payment_intent_id",
        "stripe_charge_id",
        "booking__user__email",
        "booking__user__first_name",
        "booking__user__last_name",
        "booking__id",  # Search by booking ID
        "booking__user_facing_reference",
        "booking__schedule_instance__schedule__option__classId__title",  # Search class name
        "booking__schedule_instance__schedule__option__classId__businessId__businessName",  # Search business name
    ]
    ordering_fields = [
        "created_at",
        "amount",
        "status",
        "refunded_amount",
        "booking__booking_date",
    ]
    ordering = ["-created_at"]  # Default ordering

    def get_queryset(self):
        # Base permission check
        if not self.request.user.has_perm("quickstart.view_payment"):
            logger.warning(
                f"User {self.request.user.email} denied access to list payments (missing view_payment perm)."
            )
            return Payment.objects.none()

        # Use select_related for efficiency
        queryset = Payment.objects.select_related(
            "booking__user",
            "booking__user__role",  # Include user role for hierarchy checks if needed elsewhere
            "booking__schedule_instance__schedule__option__classId__businessId",
        ).all()

        # --- Filtering Logic ---
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        payment_method = self.request.query_params.get("payment_method")
        if payment_method:
            queryset = queryset.filter(payment_method_type=payment_method)

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date and end_date:
            try:  # Add basic validation for date format
                start_dt = timezone.datetime.strptime(start_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                # Include the whole end day
                end_dt = timezone.datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
                queryset = queryset.filter(created_at__range=[start_dt, end_dt])
            except ValueError:
                logger.warning(
                    f"Invalid date format received: start={start_date}, end={end_date}"
                )
                # Optionally return an error or just ignore invalid dates

        # Search and Ordering are handled by filter backends

        return queryset

    # --- Standard Actions Overridden ---

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Allow deleting payment records if permitted"""
        if not request.user.has_perm("quickstart.delete_payment"):
            self.permission_denied(
                request, message="You do not have permission to delete payment records."
            )

        instance = self.get_object()
        # Hierarchy check
        if instance.booking and instance.booking.user:
            if not user_can_manage(request.user, instance.booking.user):
                self.permission_denied(
                    request,
                    message="You cannot delete this payment due to hierarchy restrictions.",
                )

        logger.warning(
            f"Payment ID {instance.pk} (Intent: {instance.stripe_payment_intent_id}) deleted by Admin {request.user.email}"
        )
        # Add to AuditLog if needed
        return super().destroy(request, *args, **kwargs)

    # --- Custom Actions ---

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """Get payment statistics for admin dashboard"""
        if not request.user.has_perm("quickstart.view_payment_stats"):
            self.permission_denied(
                request, message="You cannot view payment statistics."
            )

        try:
            # --- Calculation logic remains the same ---
            end_date = timezone.now()
            days = int(request.query_params.get("days", 30))  # Allow specifying days
            start_date = end_date - timedelta(days=days)

            payments = Payment.objects.filter(created_at__range=[start_date, end_date])
            total_revenue = payments.filter(status="succeeded").aggregate(
                total=Coalesce(Sum("amount"), Decimal(0))  # Use Coalesce for Sum
            )["total"]

            previous_start = start_date - timedelta(days=days)
            previous_revenue = Payment.objects.filter(
                created_at__range=[
                    previous_start,
                    start_date,
                ],  # Compare correct previous period
                status="succeeded",
            ).aggregate(total=Coalesce(Sum("amount"), Decimal(0)))["total"]

            revenue_growth = 0
            if previous_revenue > 0:
                revenue_growth = (
                    (total_revenue - previous_revenue) / previous_revenue
                ) * 100

            # Use base queryset to get pending count
            pending_payments = Payment.objects.filter(
                status="pending"
            ).count()  # Overall pending

            refunded_amount = payments.filter(
                status__in=["refunded", "partially_refunded"]
            ).aggregate(total=Coalesce(Sum("refunded_amount"), Decimal(0)))["total"]

            total_transactions = payments.count()
            successful_transactions = payments.filter(status="succeeded").count()

            return Response(
                {
                    "total_revenue": float(total_revenue),
                    "revenue_growth": round(revenue_growth, 1),
                    "pending_payments": pending_payments,
                    "refunded_amount": float(refunded_amount),
                    "total_transactions": total_transactions,
                    "successful_transactions": successful_transactions,
                    "period_days": days,
                }
            )
        except Exception as e:
            logger.error(f"Error getting payment stats: {str(e)}", exc_info=True)
            return Response(
                {"error": "Failed to retrieve payment statistics"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated, CanAccessPaymentAdmin],
    )
    def refund(self, request, pk=None):
        """Process refund through Stripe for a specific payment"""
        # 1. Check base permission for processing refunds
        if not request.user.has_perm("quickstart.process_refund"):
            self.permission_denied(
                request, message="You do not have permission to process refunds."
            )

        payment = self.get_object()  # Get payment via pk

        # 2. Perform Hierarchy Check ONLY if user is NOT a Super Admin
        is_super_admin = request.user.role and request.user.role.name == "Super Admin"
        if not is_super_admin:
            # Check if the payment is linked to a user and apply hierarchy check
            if payment.booking and payment.booking.user:
                if not user_can_manage(request.user, payment.booking.user):
                    self.permission_denied(
                        request,
                        message="You cannot manage this payment due to hierarchy restrictions.",
                    )
            # else: # Decide how to handle payments not linked to a user - allow non-super admins?
            #     pass # Or add a specific check/denial if needed

        # --- Proceed with Refund Logic (from previous step) ---
        amount_str = request.data.get(
            "amount"
        )  # Refund amount optional, defaults to full
        reason = request.data.get("reason", "requested_by_customer")  # Optional reason

        # 3. Validation
        if payment.status not in ["succeeded", "partially_refunded"]:
            return Response(
                {
                    "error": f'Payment with status "{payment.status}" cannot be refunded.'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        available_refund = payment.available_refund_amount  # Use property
        refund_amount_decimal = available_refund  # Default to full available refund

        if amount_str:
            try:
                refund_amount_decimal = Decimal(amount_str)
                if refund_amount_decimal <= 0:
                    raise ValueError("Refund amount must be positive.")
                if refund_amount_decimal > available_refund:
                    return Response(
                        {
                            "error": f"Refund amount ({refund_amount_decimal}) exceeds available amount ({available_refund})."
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except (ValueError, TypeError):
                return Response(
                    {"error": "Invalid refund amount provided."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # --- 4. Stripe Interaction ---
        try:
            if abs(payment.available_refund_amount - refund_amount_decimal) < Decimal(
                "0.01"
            ):
                refund_amount_decimal = payment.available_refund_amount

            logger.info(
                f"Attempting Stripe refund for Payment Intent: {payment.stripe_payment_intent_id}, Amount: {refund_amount_decimal}, Requested by: {request.user.email} (SuperAdmin: {is_super_admin})"
            )
            refund = stripe.Refund.create(
                payment_intent=payment.stripe_payment_intent_id,
                amount=int(refund_amount_decimal * 100),  # Amount in cents
                reason=reason,
                metadata={  # Add metadata about who initiated refund
                    "refunded_by_user_id": request.user.userId,
                    "refunded_by_user_email": request.user.email,
                    "booking_id": payment.booking_id,
                },
            )
            logger.info(f"Stripe refund successful: Refund ID {refund.id}")

        except stripe.error.InvalidRequestError as e:
            logger.error(
                f"Stripe InvalidRequestError during refund for PI {payment.stripe_payment_intent_id}: {str(e)}"
            )
            return Response(
                {"error": f"Stripe Error: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST
            )
        except stripe.StripeError as e:
            logger.error(
                f"Generic StripeError during refund for PI {payment.stripe_payment_intent_id}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": f"Stripe Error: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as e:
            logger.error(
                f"Unexpected error during refund for PI {payment.stripe_payment_intent_id}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {"error": "An unexpected error occurred during refund."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # --- 5. Update Local Database ---
        try:
            with transaction.atomic():
                payment.refresh_from_db()
                if payment.status not in ["succeeded", "partially_refunded"]:
                    raise Exception(
                        f"Payment status changed unexpectedly to {payment.status} before refund update."
                    )

                payment.refunded_amount += refund_amount_decimal
                payment.status = (
                    "refunded"
                    if payment.available_refund_amount <= 0
                    else "partially_refunded"
                )
                payment.refund_date = timezone.now()
                payment.refund_reason = reason
                payment.metadata = payment.metadata or {}  # Ensure metadata exists
                payment.metadata["stripe_refund_id"] = (
                    refund.id
                )  # Store stripe refund ID if needed
                payment.save()

                if payment.booking:
                    payment.booking.payment_status = payment.status
                    payment.booking.save(update_fields=["payment_status"])

                logger.info(
                    f"Local Payment ID {payment.pk} updated after refund. New status: {payment.status}"
                )
                # Add to AuditLog
                self._log_payment_action(
                    payment,
                    "payment_refund",
                    f"Refund of {refund_amount_decimal} {payment.currency} processed. Reason: {reason}",
                    request,
                )

        except Exception as e:
            logger.error(
                f"DATABASE error updating payment {payment.pk} after successful Stripe refund {refund.id}: {str(e)}",
                exc_info=True,
            )
            return Response(
                {
                    "error": "Stripe refund processed, but failed to update local records. Please contact support.",
                    "refund_id": refund.id,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # --- 6. Return Success Response ---
        serializer = self.get_serializer(payment)
        return Response(
            {
                "success": True,
                "message": f"Refund of {refund_amount_decimal} processed successfully.",
                "refund_id": refund.id,
                "payment": serializer.data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[
            IsAuthenticated,
            CanAccessPaymentAdmin,
            CanManageTargetPayment,
        ],
    )
    def mark_paid(self, request, pk=None):
        """Mark a pending payment as paid (e.g., for offline payments)"""
        if not request.user.has_perm("quickstart.mark_payment_paid"):
            self.permission_denied(
                request, message="You do not have permission to mark payments as paid."
            )

        payment = self.get_object()
        # Hierarchy check handled by decorator

        if payment.status != "pending":
            return Response(
                {"error": "Only pending payments can be marked as paid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            payment.status = "succeeded"
            # Optionally add details about manual confirmation
            payment.metadata = payment.metadata or {}
            payment.metadata["marked_paid_by"] = request.user.email
            payment.metadata["marked_paid_at"] = timezone.now().isoformat()
            payment.save(update_fields=["status", "metadata"])  # Optimize save

            # Update related booking
            if payment.booking:
                payment.booking.payment_status = "paid"
                # Consider if booking status should change from 'pending' to 'confirmed'
                if payment.booking.status == "pending":
                    payment.booking.status = "confirmed"
                    payment.booking.save(update_fields=["payment_status", "status"])
                else:
                    payment.booking.save(update_fields=["payment_status"])

        logger.info(
            f"Payment ID {payment.pk} marked as paid by Admin {request.user.email}"
        )
        # Add to AuditLog
        self._log_payment_action(
            payment, "payment_manual_paid", "Payment marked as paid by admin", request
        )

        return Response(self.get_serializer(payment).data)

    # Read-only actions, only need base view permission
    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        """Get payment history/timeline events"""
        if not request.user.has_perm("quickstart.view_payment"):
            self.permission_denied(request, message="You cannot view payment history.")

        payment = self.get_object()
        # Hierarchy check could be added if history is sensitive
        # if payment.booking and payment.booking.user:
        #    if not user_can_manage(request.user, payment.booking.user): ...

        # --- Simulation logic remains the same ---
        history = [...]  # Build history as before
        return Response(history)

    @action(detail=True, methods=["get"])
    def receipt(self, request, pk=None):
        """Get payment receipt URL"""
        if not request.user.has_perm("quickstart.view_payment"):
            self.permission_denied(request, message="You cannot view payment receipts.")

        payment = self.get_object()
        # Hierarchy check could be added
        # if payment.booking and payment.booking.user:
        #    if not user_can_manage(request.user, payment.booking.user): ...

        if not payment.receipt_url:
            return Response(
                {"error": "No receipt available for this payment."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"receipt_url": payment.receipt_url})

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Export payments data to CSV"""
        if not request.user.has_perm("quickstart.export_payment_data"):
            self.permission_denied(request, message="You cannot export payment data.")

        try:
            queryset = self.filter_queryset(self.get_queryset())  # Apply filters

            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = (
                'attachment; filename="payments_export.csv"'
            )
            writer = csv.writer(response)

            # Write header (adjust based on desired fields)
            writer.writerow(
                [
                    "Payment ID",
                    "Intent ID",
                    "Amount",
                    "Currency",
                    "Status",
                    "Method",
                    "Card Brand",
                    "Card Last4",
                    "Created At",
                    "Booking ID",
                    "Customer Name",
                    "Customer Email",
                    "Business Name",
                    "Class Name",
                    "Refunded Amount",
                    "Refund Date",
                    "Receipt URL",
                ]
            )

            # Write data efficiently using values_list or values
            payment_data = queryset.values_list(
                "id",
                "stripe_payment_intent_id",
                "amount",
                "currency",
                "status",
                "payment_method_type",
                "card_brand",
                "card_last4",
                "created_at",
                "booking__id",  # Direct relation
                "booking__user__first_name",
                "booking__user__last_name",
                "booking__user__email",  # User info
                # Class/Business Info - Adjust field paths as per your model relations
                "booking__schedule_instance__schedule__option__classId__businessId__businessName",
                "booking__schedule_instance__schedule__option__classId__title",
                "refunded_amount",
                "refund_date",
                "receipt_url",
            )

            for pmt in payment_data:
                writer.writerow(
                    [
                        pmt[0],  # id
                        pmt[1],  # intent_id
                        pmt[2],  # amount
                        pmt[3],  # currency
                        pmt[4],  # status
                        pmt[5],  # method
                        pmt[6] or "",  # card_brand
                        pmt[7] or "",  # card_last4
                        (
                            pmt[8].strftime("%Y-%m-%d %H:%M:%S") if pmt[8] else ""
                        ),  # created_at
                        pmt[9] or "",  # booking_id
                        f"{pmt[10] or ''} {pmt[11] or ''}".strip(),  # customer_name
                        pmt[12] or "",  # customer_email
                        pmt[13] or "",  # business_name
                        pmt[14] or "",  # class_name
                        pmt[15] or 0.0,  # refunded_amount
                        (
                            pmt[16].strftime("%Y-%m-%d %H:%M:%S") if pmt[16] else ""
                        ),  # refund_date
                        pmt[17] or "",  # receipt_url
                    ]
                )

            return response

        except Exception as e:
            logger.error(f"Error exporting payments: {str(e)}", exc_info=True)
            # Return a user-friendly error in the response if possible
            return HttpResponse(
                f"Error exporting data: {str(e)}", status=500, content_type="text/plain"
            )

    def _log_payment_action(self, payment, action_code, details, request):
        """Helper to log payment related actions"""
        try:
            target_user = payment.booking.user if payment.booking else None
            AuditLog.objects.create(
                user=request.user,
                user_email=request.user.email,
                action=action_code,
                details=details,
                target_user=target_user,
                target_model="Payment",
                target_id=str(payment.id),
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                metadata={
                    "payment_intent_id": payment.stripe_payment_intent_id,
                    "amount": float(payment.amount),
                    "currency": payment.currency,
                    "booking_id": payment.booking_id,
                },
            )
        except Exception as e:
            logger.error(
                f"Failed to create audit log for payment action {action_code}: {str(e)}",
                exc_info=True,
            )
