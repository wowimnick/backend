# quickstart/views/business/business_payout_views.py
from decimal import Decimal
from django.db.models import Q, Sum, Value, Count
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from quickstart.models import BusinessInfo, Booking, Payout
from quickstart.serializers.business.business_payout_serializers import (
    BusinessPayoutSerializer,
    PayoutSummarySerializer,
    PayoutBookingSerializer,
)
from quickstart.utils.permissions import IsBusinessOwnerOrManager
from quickstart.views.business.business_booking_views import BusinessBookingPagination

import logging

logger = logging.getLogger(__name__)


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

    @action(detail=True, methods=["get"], url_path="bookings")
    def bookings(self, request, pk=None):
        """
        Retrieves a paginated list of all bookings associated with a specific payout.
        """
        payout = self.get_object()

        business = self.get_business_context()
        if payout.business != business:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

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
        Provides a summary of the business's current payout status.
        """
        business = self.get_business_context()

        pending_bookings = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status="completed",
            payment_status="paid",
            payout_status="pending",
        ).prefetch_related("payments")

        pending_payout_amount = Decimal("0.0")
        for booking in pending_bookings:
            payment = booking.payments.first()
            if payment:
                service_fee = payment.service_fee_amount or Decimal("0.00")
                net_amount = booking.amount_paid - service_fee
                pending_payout_amount += net_amount

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
