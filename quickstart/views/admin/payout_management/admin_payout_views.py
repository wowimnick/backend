from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import Sum, Count, Q, F, Avg
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import logging
import csv
from django.http import HttpResponse

from quickstart.utils.permissions import IsAuthenticated, BasePermission, CanAccessPayoutAdmin
from quickstart.models import Payout
from quickstart.serializers.admin.payout_management.admin_payout_serializers import (
    AdminPayoutListSerializer,
    AdminPayoutDetailSerializer,
)

from quickstart.tasks import process_daily_payouts

logger = logging.getLogger(__name__)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminPayoutViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin-only viewset for viewing and managing business payouts.
    Creation is handled by an automated background process, so this is ReadOnly.
    Manual actions are provided via the @action decorator.
    """

    permission_classes = [IsAuthenticated, CanAccessPayoutAdmin]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["business__businessName", "stripe_transfer_id", "id"]
    ordering_fields = ["created_at", "amount", "arrival_date", "status"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminPayoutDetailSerializer
        return AdminPayoutListSerializer

    def get_queryset(self):
        return (
            Payout.objects.select_related("business")
            .prefetch_related("bookings__payments")
            .all()
        )

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """
        Provides key metrics and statistics for the Payouts dashboard for a given period.
        """
        if not request.user.has_perm("quickstart.view_payout_analytics"):
            self.permission_denied(request, message="You cannot view payout analytics.")

        try:
            start_param = request.query_params.get("start_date")
            end_param = request.query_params.get("end_date")

            if start_param and end_param:
                start_date = timezone.datetime.strptime(start_param, "%Y-%m-%d").date()
                end_date = timezone.datetime.strptime(end_param, "%Y-%m-%d").date()
            else:
                end_date = timezone.localdate()
                start_date = end_date - timedelta(days=29)

            payouts_in_period = Payout.objects.filter(
                created_at__date__range=[start_date, end_date]
            )

            aggregates = payouts_in_period.aggregate(
                total_paid_out=Coalesce(
                    Sum("amount", filter=Q(status="paid")), Decimal(0)
                ),
                payouts_pending=Count("id", filter=Q(status="pending"))
                + Count("id", filter=Q(status="in_transit")),
                payouts_failed=Count("id", filter=Q(status="failed")),
                avg_payout_amount=Coalesce(
                    Avg("amount", filter=Q(status="paid")), Decimal(0)
                ),
            )

            businesses_paid_count = (
                payouts_in_period.filter(status="paid")
                .values("business")
                .distinct()
                .count()
            )

            response_data = {
                "total_paid_out": aggregates["total_paid_out"],
                "payouts_pending": aggregates["payouts_pending"],
                "payouts_failed": aggregates["payouts_failed"],
                "businesses_paid_count": businesses_paid_count,
                "average_payout_amount": aggregates["avg_payout_amount"],
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d"),
            }
            return Response(response_data)

        except Exception as e:
            logger.error(f"Error in payout analytics: {e}", exc_info=True)
            return Response(
                {"error": "Failed to retrieve payout analytics"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["post"], url_path="retry")
    def retry_failed_payout(self, request, pk=None):
        """
        Resets a failed payout's status to 'pending', allowing the nightly
        Celery task to attempt processing it again.
        """
        if not request.user.has_perm("quickstart.retry_failed_payout"):
            self.permission_denied(
                request, message="You do not have permission to retry payouts."
            )

        payout = self.get_object()
        if payout.status != "failed":
            return Response(
                {"error": "Only failed payouts can be retried."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info(
            f"Admin {request.user.email} initiated retry for failed payout {payout.id}"
        )

        # Reset the status. The nightly task will handle the rest.
        payout.status = "pending"
        payout.save(update_fields=["status"])

        return Response(
            {"message": f"Payout {payout.id} has been re-queued for processing."},
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="trigger-manual")
    def trigger_manual_payout(self, request):
        """
        Manually triggers the daily payout Celery task to run immediately.
        """
        if not request.user.has_perm("quickstart.trigger_manual_payout"):
            self.permission_denied(
                request, message="You do not have permission to trigger manual payouts."
            )

        logger.info(f"Admin {request.user.email} triggered manual payout process.")

        # Asynchronously call the Celery task
        process_daily_payouts.delay()

        return Response(
            {"message": "Manual payout process has been initiated in the background."},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=False, methods=["get"])
    def export(self, request):
        """
        Exports the currently filtered list of payouts to a CSV file.
        """
        if not request.user.has_perm("quickstart.export_payout_data"):
            self.permission_denied(request, message="You cannot export payout data.")

        try:
            queryset = self.filter_queryset(self.get_queryset())
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = (
                f'attachment; filename="payouts_export_{timezone.now().strftime("%Y-%m-%d")}.csv"'
            )

            writer = csv.writer(response)
            writer.writerow(
                [
                    "Payout ID",
                    "Stripe Transfer ID",
                    "Business Name",
                    "Amount",
                    "Currency",
                    "Status",
                    "Date Initiated",
                    "Expected Arrival Date",
                    "Included Bookings Count",
                ]
            )

            for payout in queryset:
                writer.writerow(
                    [
                        str(payout.id),
                        payout.stripe_transfer_id,
                        payout.business.businessName,
                        payout.amount,
                        payout.currency,
                        payout.status,
                        payout.created_at.strftime("%Y-%m-%d %H:%M"),
                        payout.arrival_date.strftime("%Y-%m-%d"),
                        payout.bookings.count(),
                    ]
                )
            return response
        except Exception as e:
            logger.error(f"Error exporting payouts: {e}", exc_info=True)
            return HttpResponse(
                f"Error exporting data: {e}", status=500, content_type="text/plain"
            )
