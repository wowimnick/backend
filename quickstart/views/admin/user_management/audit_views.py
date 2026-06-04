from rest_framework import viewsets, filters, status, pagination
from rest_framework.response import Response
from rest_framework.decorators import action
from django.utils import timezone
from django.db.models import Count
from django.contrib.auth import get_user_model
import datetime
import csv
from django.http import HttpResponse
import logging

from quickstart.serializers.admin.user_management.audit_serializers import (
    AuditLogSerializer,
)

from quickstart.models import AuditLog
from quickstart.utils.permissions import IsAuthenticated, CanAccessUserAdmin
from quickstart.utils.admin_export import check_export_row_limit, export_row_limit_response


User = get_user_model()
logger = logging.getLogger(__name__)


class StandardResultsSetPagination(pagination.PageNumberPagination):
    """Standard pagination for better performance"""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Viewset for audit logs - read-only access for admins"""

    permission_classes = [IsAuthenticated, CanAccessUserAdmin]
    serializer_class = AuditLogSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["user_email", "details", "ip_address"]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        # Create base queryset with optimized joins to reduce N+1 queries
        queryset = AuditLog.objects.select_related(
            "user",
            "user__role",
            "target_user",
            "target_user__role",
        )

        # Filter by action
        action = self.request.query_params.get("action")
        if action and action != "all":
            queryset = queryset.filter(action=action)

        # Filter by user
        user_id = self.request.query_params.get("user_id")
        if user_id:
            queryset = queryset.filter(user_id=user_id)

        # Filter by date range
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")

        if start_date and end_date:
            try:
                start = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                end = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
                queryset = queryset.filter(timestamp__range=(start, end))
            except ValueError:
                pass

        # Default ordering
        return queryset.order_by("-timestamp")

    @action(detail=False, methods=["get"])
    def export(self, request):
        """Export audit logs to CSV"""
        queryset = self.get_queryset()
        ok, count = check_export_row_limit(queryset)
        if not ok:
            return export_row_limit_response(count)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="audit_logs.csv"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "Timestamp",
                "User",
                "Email",
                "Action",
                "Details",
                "IP Address",
                "Target User",
                "Target Type",
                "Target ID",
            ]
        )

        # Optimize by fetching all data in a single query and then processing
        logs = queryset.select_related(
            "user",
            "user__role",
            "target_user",
            "target_user__role",
        ).values_list(
            "timestamp",
            "user__first_name",
            "user__last_name",
            "user_email",
            "action",
            "details",
            "ip_address",
            "target_user__first_name",
            "target_user__last_name",
            "target_model",
            "target_id",
        )

        # Get action choices dictionary for proper display labels
        action_choices = dict(AuditLog.ACTION_CHOICES)

        for log in logs:
            # Get the proper display label for the action
            action_text = action_choices.get(log[4], log[4].replace("_", " ").title())

            writer.writerow(
                [
                    log[0].strftime("%Y-%m-%d %H:%M:%S"),
                    f"{log[1]} {log[2]}" if log[1] and log[2] else "",
                    log[3],
                    action_text,  # Use properly formatted action text
                    log[5],
                    log[6],
                    f"{log[7]} {log[8]}" if log[7] and log[8] else "",
                    log[9],
                    log[10],
                ]
            )

        return response

    @action(detail=False, methods=["get"])
    def activity_summary(self, request):
        """Get summary of user activity"""
        # Get date range
        days = int(request.query_params.get("days", 30))
        end_date = timezone.now()
        start_date = end_date - datetime.timedelta(days=days)

        # Get counts by action type
        action_counts = (
            AuditLog.objects.filter(timestamp__range=(start_date, end_date))
            .values("action")
            .annotate(count=Count("id"))
            .order_by("-count")
        )

        # Get most active users using a more efficient query
        active_users = (
            AuditLog.objects.filter(timestamp__range=(start_date, end_date))
            .values("user_id", "user_email")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        # Get daily activity trend
        daily_activity = (
            AuditLog.objects.filter(timestamp__range=(start_date, end_date))
            .extra(select={"day": "date(timestamp)"})
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        # Get proper action labels from choices
        action_choices = dict(AuditLog.ACTION_CHOICES)

        return Response(
            {
                "action_counts": [
                    {
                        "action": item["action"],
                        "action_label": action_choices.get(
                            item["action"], item["action"].replace("_", " ").title()
                        ),
                        "count": item["count"],
                    }
                    for item in action_counts
                ],
                "active_users": active_users,
                "daily_activity": [
                    {"date": item["day"].strftime("%Y-%m-%d"), "count": item["count"]}
                    for item in daily_activity
                ],
            }
        )
