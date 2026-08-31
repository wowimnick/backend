from datetime import date, timedelta
from decimal import Decimal


def _payout_payment_meta_source(meta):
    if not meta or not isinstance(meta, dict):
        return {}
    inner = meta.get("original_stripe_metadata")
    if isinstance(inner, dict):
        return inner
    return meta
from django.conf import settings as django_settings
from django.db.models import Q, Sum, Count
from django.db.models.functions import Coalesce
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django.http import HttpResponse
import csv
import stripe

from quickstart.models import BusinessInfo, Booking, Payment, Payout
from quickstart.serializers.business.business_payout_serializers import (
    BusinessPayoutSerializer,
    PayoutSummarySerializer,
    PayoutBookingSerializer,
)
from quickstart.utils.permissions import IsBusinessOwnerOrManager
from quickstart.views.business.business_booking_views import BusinessBookingPagination

import logging

logger = logging.getLogger(__name__)
stripe.api_key = django_settings.STRIPE_SECRET_KEY

SCHEDULED_PAYOUT_PREFIX = "scheduled-"


def _connect_pending_amount(business):
    """Stripe Connect pending balance for the business currency, or None if unavailable."""
    if not business or not business.stripe_account_id:
        return None
    try:
        balance = stripe.Balance.retrieve(stripe_account=business.stripe_account_id)
    except Exception as e:
        logger.warning(
            "Connect balance lookup failed for business %s: %s",
            business.businessId,
            e,
        )
        return None
    currency = (business.currency or "CAD").upper()
    pending = Decimal("0.00")
    for item in getattr(balance, "pending", []) or []:
        item_currency = (item.get("currency") or "").upper()
        if item_currency == currency:
            pending += Decimal(item.get("amount", 0) or 0) / 100
    return pending


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
        Provides a summary of the business's current payout status.
        Prefers Stripe Connect pending balance when the account is linked.
        last_payout_* always comes from paid Payout rows.
        """
        business = self.get_business_context()

        pending_payout_amount = _connect_pending_amount(business)
        if pending_payout_amount is None:
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
                "Tax Collected",
                "Platform Fee (Pre-tax)",
                "HST on Platform Fee",
                "Net Payout for this Booking",
                "Business Discount ($)",
                "Global Discount ($)",
                "Gift Card Applied ($)",
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
            .prefetch_related(
                "booking__applieddiscount_set",
                "booking__applied_global_discounts",
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

            biz_disc = sum(
                (ad.amount_saved for ad in booking.applieddiscount_set.all()),
                start=Decimal("0.00"),
            )
            glob_disc = sum(
                (a.amount_saved for a in booking.applied_global_discounts.all()),
                start=Decimal("0.00"),
            )
            src = _payout_payment_meta_source(payment.metadata)
            try:
                gc_raw = Decimal(str(src.get("gift_card_amount_to_deduct") or "0"))
            except Exception:
                gc_raw = Decimal("0.00")
            if not src.get("paid_via_giftcard") and gc_raw == 0:
                gift_amt = Decimal("0.00")
            elif payment.amount and payment.amount > 0:
                gift_amt = (
                    gc_raw * (booking.amount_paid / payment.amount)
                ).quantize(Decimal("0.01"))
            else:
                gift_amt = gc_raw.quantize(Decimal("0.01"))

            if payment.amount and payment.amount > 0:
                pay_share = (booking.amount_paid / payment.amount).quantize(
                    Decimal("0.0001")
                )
                tax_shown = (payment.tax_amount * pay_share).quantize(Decimal("0.01"))
                fee_shown = (payment.platform_fee_amount * pay_share).quantize(
                    Decimal("0.01")
                )
                fee_tax_shown = (payment.platform_fee_tax * pay_share).quantize(
                    Decimal("0.01")
                )
                net_shown = (payment.net_payout_amount * pay_share).quantize(
                    Decimal("0.01")
                )
            else:
                tax_shown = payment.tax_amount
                fee_shown = payment.platform_fee_amount
                fee_tax_shown = payment.platform_fee_tax
                net_shown = payment.net_payout_amount

            writer.writerow(
                [
                    booking.user_facing_reference or f"ID-{booking.id}",
                    booking.booking_date.strftime("%Y-%m-%d"),
                    booking.schedule_instance.schedule.option.classId.title,
                    booker_name,
                    f"${booking.amount_paid:.2f}",
                    f"${tax_shown:.2f}",
                    f"${fee_shown:.2f}",
                    f"${fee_tax_shown:.2f}",
                    f"${net_shown:.2f}",
                    f"${biz_disc:.2f}",
                    f"${glob_disc:.2f}",
                    f"${gift_amt:.2f}",
                ]
            )
            total_payout_from_bookings += net_shown

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
                "",
                "",
                "",
                "",
                "Total from Bookings:",
                f"${total_payout_from_bookings:.2f}",
            ]
        )
        writer.writerow(
            [
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "Total Payout Amount:",
                f"${payout.amount:.2f}",
            ]
        )

        # Check if the sum matches the payout total
        if total_payout_from_bookings.quantize(
            Decimal("0.01")
        ) == payout.amount.quantize(Decimal("0.01")):
            status_text = "Reconciled"
        else:
            status_text = "Discrepancy Found"
        writer.writerow(
            ["", "", "", "", "", "", "", "", "", "", "Status:", status_text]
        )

        logger.info(
            f"Payout report {payout.id} exported for Business '{business.businessName}' by {request.user.email}"
        )
        return response
