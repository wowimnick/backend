"""
Public endpoint to fetch the currently active global discount for display on checkout.
No authentication required.
"""

from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Q, F

from quickstart.models import GlobalDiscount


class ActiveGlobalDiscountView(APIView):
    """
    GET: Returns the currently active global discount, if any.
    Query params:
      - subtotal (optional): If provided, returns calculated_discount_amount for that subtotal.
    """

    permission_classes = []

    def get(self, request):
        now = timezone.now()
        active = (
            GlobalDiscount.objects.filter(is_active=True)
            .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now))
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=now))
            .filter(Q(usage_limit__isnull=True) | Q(usage_count__lt=F("usage_limit")))
            .order_by("-created_at")
            .first()
        )
        if not active:
            return Response({"active_discount": None})

        subtotal_str = request.query_params.get("subtotal")
        calculated_amount = None
        if subtotal_str is not None:
            try:
                subtotal = Decimal(subtotal_str)
                if subtotal > 0:
                    valid, _ = active.is_valid_for_amount(subtotal)
                    if valid:
                        if active.discount_type == "percentage":
                            calculated_amount = (
                                subtotal * (active.value / Decimal(100))
                            ).quantize(Decimal("0.01"))
                        else:
                            calculated_amount = min(active.value, subtotal)
            except (ValueError, TypeError):
                pass

        return Response(
            {
                "active_discount": {
                    "id": str(active.id),
                    "name": active.name,
                    "discount_type": active.discount_type,
                    "value": float(active.value),
                    "min_purchase_amount": (
                        float(active.min_purchase_amount)
                        if active.min_purchase_amount is not None
                        else None
                    ),
                    "calculated_discount_amount": (
                        float(calculated_amount) if calculated_amount is not None else None
                    ),
                }
            }
        )
