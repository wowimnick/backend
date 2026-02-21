from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Q, Sum, Value, Count
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, NotFound
from django.http import HttpResponse
import csv

from quickstart.models import BusinessInfo, Booking, Payment, Payout
from quickstart.serializers.business.business_payout_serializers import (
    BusinessPayoutSerializer,
    PayoutSummarySerializer,
    PayoutBookingSerializer,
    ScheduledPayoutSerializer,
)
from quickstart.utils.permissions import IsBusinessOwnerOrManager
from quickstart.views.business.business_booking_views import BusinessBookingPagination

import logging

logger = logging.getLogger(__name__)

SCHEDULED_PAYOUT_PREFIX = "scheduled-"


def get_scheduled_payouts_for_business(business):
    """
    Compute scheduled (projected) payouts: confirmed, paid, future-dated bookings
    grouped by payout date (session date + 1 day). Payout is due the day after the experience.
    """
    today = timezone.now().date()
    qs = (
        Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status="confirmed",
            payment_status="paid",
            payout_status="pending",
            schedule_instance__date__gte=today,
        )
        .select_related("schedule_instance")
        .order_by("schedule_instance__date")
    )
    groups = defaultdict(lambda: {"amount": Decimal("0.00"), "count": 0, "arrival_date": None})
    for b in qs:
        if b.schedule_instance and b.schedule_instance.date:
            arr = b.schedule_instance.date + timedelta(days=1)
            groups[arr]["arrival_date"] = arr
            groups[arr]["amount"] += b.allocated_net_payout or Decimal("0.00")
            groups[arr]["count"] += 1
    return [
        {
            "id": f"{SCHEDULED_PAYOUT_PREFIX}{arr.isoformat()}",
            "stripe_transfer_id": None,
            "amount": data["amount"],
            "currency": business.currency.upper(),
            "status": "scheduled",
            "arrival_date": data["arrival_date"],
            "created_at": None,
            "booking_count": data["count"],
            "amount_display": f"${data['amount']:,.2f} {business.currency.upper()}",
        }
        for arr, data in sorted(groups.items())
        if data["count"] > 0 and data["amount"] > 0
    ]


class BusinessPayoutViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides business users with access to their payout history and a summary of their current financial status.
    """

    serializer_class = BusinessPayoutSerializer
    permission_classes = [IsAuthenticated, IsBusinessOwnerOrManager]
    pagination_class = BusinessBookingPagination

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
        business = self.get_business_context()
        return (
            Payout.objects.filter(business=business)
            .annotate(booking_count_agg=Count("bookings"))
            .order_by("-created_at")
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        business = self.get_business_context()
        scheduled = get_scheduled_payouts_for_business(business)
        response.data["scheduled_payouts"] = ScheduledPayoutSerializer(
            scheduled, many=True
        ).data
        return response

    @action(detail=True, methods=["get"], url_path="bookings")
    def bookings(self, request, pk=None):
        """
        Retrieves a paginated list of all bookings associated with a specific payout.
        For scheduled payouts (pk like 'scheduled-YYYY-MM-DD'), returns bookings
        whose session date + 1 day equals that date.
        """
        business = self.get_business_context()

        if pk and str(pk).startswith(SCHEDULED_PAYOUT_PREFIX):
            try:
                date_str = str(pk).replace(SCHEDULED_PAYOUT_PREFIX, "")
                arrival_date = date.fromisoformat(date_str)
            except (ValueError, TypeError):
                return Response(
                    {"detail": "Invalid scheduled payout id."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            session_date = arrival_date - timedelta(days=1)
            bookings_queryset = (
                Booking.objects.filter(
                    schedule_instance__schedule__option__classId__businessId=business,
                    status="confirmed",
                    payment_status="paid",
                    payout_status="pending",
                    schedule_instance__date=session_date,
                )
                .select_related(
                    "user", "schedule_instance__schedule__option__classId"
                )
                .prefetch_related("payments")
                .order_by("-booking_date")
            )
        else:
            payout = self.get_object()
            if payout.business != business:
                return Response(
                    {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
                )
            bookings_queryset = (
                payout.bookings.select_related(
                    "user", "schedule_instance__schedule__option__classId"
                )
                .prefetch_related("payments")
                .order_by("-booking_date")
            )

        page = self.paginate_queryset(bookings_queryset)
        if page is not None:
            serializer = PayoutBookingSerializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)

        serializer = PayoutBookingSerializer(
            bookings_queryset, many=True, context={"request": request}
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request, *args, **kwargs):
        """
        Provides a summary of the business's current payout status using
        booking-based calculations.
        """
        business = self.get_business_context()

        # Calculate pending payouts by summing the allocated share of COMPLETED bookings
        # that haven't been paid out yet.
        pending_payout_aggregation = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status__in=["confirmed", "completed", "forfeited"],
            payment_status="paid",    
            payout_status="pending",  
        ).aggregate(
            total_pending=Coalesce(Sum("allocated_net_payout"), Decimal("0.00"))
        )
        
        pending_payout_amount = pending_payout_aggregation["total_pending"]

        last_payout = (
            Payout.objects.filter(business=business, status="paid")
            .order_by("-created_at")
            .first()
        )

        payouts_enabled = business.stripe_account_status == "active"

        summary_data = {
            "pending_payout_amount": pending_payout_amount,
            "last_payout_amount": last_payout.amount if last_payout else Decimal("0.0"),
            "last_payout_date": last_payout.arrival_date if last_payout else None,
            "payouts_enabled": payouts_enabled,
            "stripe_account_status": business.stripe_account_status or "unlinked",
            "stripe_account_id": business.stripe_account_id,
            "currency": business.currency,
        }

        serializer = PayoutSummarySerializer(summary_data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="export")
    def export_payout_details(self, request, pk=None):
        """
        Exports a CSV file detailing all transactions included in a specific payout.
        Scheduled payouts cannot be exported until the payout has been processed.
        """
        if pk and str(pk).startswith(SCHEDULED_PAYOUT_PREFIX):
            return Response(
                {
                    "detail": "Export is available after the payout has been processed. Scheduled payouts cannot be exported yet."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        payout = self.get_object()
        business = self.get_business_context()

        # Security check: ensure the user has permission for this payout's business
        if payout.business != business:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        response = HttpResponse(content_type="text/csv")
        filename = f"Payout_{payout.arrival_date.strftime('%Y-%m-%d')}_{payout.id}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        writer = csv.writer(response)

        # --- Report Header ---
        writer.writerow(["Payout Reconciliation Report"])
        writer.writerow(["Business:", business.businessName])
        writer.writerow(["Payout ID:", payout.id])
        writer.writerow(["Stripe Transfer ID:", payout.stripe_transfer_id])
        writer.writerow(
            [
                "Payout Date (Initiated):",
                payout.created_at.strftime("%Y-%m-%d %H:%M:%S") + " UTC",
            ]
        )
        writer.writerow(
            ["Expected Arrival Date:", payout.arrival_date.strftime("%Y-%m-%d")]
        )
        writer.writerow(["Total Payout Amount:", f"${payout.amount:.2f}"])
        writer.writerow([])  # Spacer

        # --- Detailed Transaction List Header ---
        writer.writerow(["Included Transactions"])
        writer.writerow(
            [
                "Booking Reference",
                "Booking Date",
                "Class Name",
                "Booker Name",
                "Total Amount Paid by Customer",
                "Platform Fee (Pre-tax)",
                "HST on Platform Fee",
                "Net Payout for this Booking",
            ]
        )

        # --- BUG FIX: Query from the Payment model directly for financial accuracy ---
        # This is more robust as it starts from the financial record (Payment)
        # which is guaranteed to have the correct status and amounts for the payout.
        payments_in_payout = (
            Payment.objects.filter(booking__payouts=payout, status="succeeded")
            .select_related(
                "booking__user",
                "booking__contact",
                "booking__schedule_instance__schedule__option__classId",
            )
            .order_by("booking__booking_date")
        )

        total_payout_from_bookings = Decimal("0.00")

        for payment in payments_in_payout:
            booking = payment.booking  # Get the associated booking from the payment

            booker_name = "N/A"
            if booking.user:
                booker_name = (
                    f"{booking.user.first_name} {booking.user.last_name}".strip()
                )
            elif booking.contact:
                booker_name = (
                    f"{booking.contact.first_name} {booking.contact.last_name}".strip()
                )

            writer.writerow(
                [
                    booking.user_facing_reference or f"ID-{booking.id}",
                    booking.booking_date.strftime("%Y-%m-%d"),
                    booking.schedule_instance.schedule.option.classId.title,
                    booker_name,
                    f"${payment.amount:.2f}",
                    f"${payment.platform_fee_amount:.2f}",
                    f"${payment.platform_fee_tax:.2f}",
                    f"${payment.net_payout_amount:.2f}",
                ]
            )
            total_payout_from_bookings += payment.net_payout_amount

        # --- Footer for Reconciliation ---
        writer.writerow([])  # Spacer
        writer.writerow(
            [
                "",
                "",
                "",
                "",
                "",
                "",
                "Total from Bookings:",
                f"${total_payout_from_bookings:.2f}",
            ]
        )
        writer.writerow(
            ["", "", "", "", "", "", "Total Payout Amount:", f"${payout.amount:.2f}"]
        )

        # Check if the sum matches the payout total
        if total_payout_from_bookings.quantize(
            Decimal("0.01")
        ) == payout.amount.quantize(Decimal("0.01")):
            status_text = "Reconciled"
        else:
            status_text = "Discrepancy Found"
        writer.writerow(["", "", "", "", "", "", "Status:", status_text])

        logger.info(
            f"Payout report {payout.id} exported for Business '{business.businessName}' by {request.user.email}"
        )
        return response
