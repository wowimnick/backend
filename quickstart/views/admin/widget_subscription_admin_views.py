"""
Admin view for listing and managing widget subscriptions across businesses.
"""
from rest_framework import viewsets, status
from rest_framework.response import Response

from quickstart.models import WidgetSubscription
from quickstart.utils.permissions import IsAuthenticated, CanAccessBusinessAdmin


class AdminWidgetSubscriptionViewSet(viewsets.ViewSet):
    """
    List widget subscriptions for all businesses. Admin only.
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]

    def list(self, request):
        subs = (
            WidgetSubscription.objects.all()
            .select_related("business")
            .order_by("-created_at")
        )
        out = []
        for sub in subs:
            out.append({
                "id": str(sub.id),
                "business_id": sub.business_id,
                "business_name": sub.business.businessName if sub.business else None,
                "business_slug": sub.business.slug if sub.business else None,
                "plan_id": sub.plan_id,
                "status": sub.status,
                "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
                "cancel_at_period_end": sub.cancel_at_period_end,
                "stripe_subscription_id": sub.stripe_subscription_id,
                "created_at": sub.created_at.isoformat() if sub.created_at else None,
            })
        return Response(out, status=status.HTTP_200_OK)
