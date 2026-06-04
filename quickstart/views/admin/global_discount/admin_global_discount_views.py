from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Sum, Count
from django.db.models.functions import Coalesce

from quickstart.models import GlobalDiscount, AppliedGlobalDiscount
from quickstart.utils.permissions import IsAuthenticated, CanAccessGlobalDiscountAdmin
from quickstart.utils.admin_pagination import AdminStandardPagination
from quickstart.serializers.admin.global_discount_serializers import (
    AdminGlobalDiscountSerializer,
    AdminGlobalDiscountStatsSerializer,
)


class AdminGlobalDiscountViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for platform-wide global discounts.
    List, create, retrieve, update, delete.
    GET .../global-discounts/<id>/stats/ for usage and revenue impact stats.
    """

    permission_classes = [IsAuthenticated, CanAccessGlobalDiscountAdmin]
    pagination_class = AdminStandardPagination
    serializer_class = AdminGlobalDiscountSerializer
    queryset = GlobalDiscount.objects.all().order_by("-created_at")
    lookup_field = "id"
    lookup_value_regex = "[0-9a-f-]+"

    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, id=None):
        """Return usage count, total amount saved, and number of bookings for this global discount."""
        instance = self.get_object()
        agg = AppliedGlobalDiscount.objects.filter(global_discount=instance).aggregate(
            total_amount_saved=Coalesce(Sum("amount_saved"), Decimal("0.00")),
            bookings_count=Count("booking"),
        )
        data = {
            "usage_count": instance.usage_count,
            "total_amount_saved": agg["total_amount_saved"],
            "bookings_count": agg["bookings_count"],
        }
        serializer = AdminGlobalDiscountStatsSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.data)
