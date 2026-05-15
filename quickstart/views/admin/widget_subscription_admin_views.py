"""
Admin view for listing and managing widget subscriptions across businesses.
"""
from django.utils import timezone

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from quickstart.models import WidgetSubscription, BusinessInfo, AuditLog
from quickstart.utils.permissions import IsAuthenticated, CanAccessBusinessAdmin


class AdminWidgetSubscriptionViewSet(viewsets.ViewSet):
    """
    List widget subscriptions for all businesses. Admin only.
    """

    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]

    def list(self, request):
        if not (
            request.user.has_perm("quickstart.view_widgetsubscription")
            or request.user.has_perm("quickstart.view_businessinfo")
        ):
            return Response(
                {
                    "detail": "Missing widget subscription or business info admin permission.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        subs = (
            WidgetSubscription.objects.all()
            .select_related("business")
            .order_by("-created_at")
        )
        out = []
        for sub in subs:
            out.append(
                {
                    "id": str(sub.id),
                    "business_id": sub.business_id,
                    "business_name": sub.business.businessName if sub.business else None,
                    "business_slug": sub.business.slug if sub.business else None,
                    "plan_id": sub.plan_id,
                    "status": sub.status,
                    "current_period_end": sub.current_period_end.isoformat()
                    if sub.current_period_end
                    else None,
                    "payment_grace_until": sub.payment_grace_until.isoformat()
                    if sub.payment_grace_until
                    else None,
                    "cancel_at_period_end": sub.cancel_at_period_end,
                    "stripe_subscription_id": sub.stripe_subscription_id,
                    "comp_reason": sub.comp_reason or "",
                    "created_at": sub.created_at.isoformat() if sub.created_at else None,
                }
            )
        return Response(out, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="comp-override")
    def comp_override(self, request):
        """Assign a complimentary widget plan without Stripe."""
        if not request.user.has_perm("quickstart.view_widgetsubscription"):
            return Response(
                {"detail": "Missing quickstart.view_widgetsubscription permission."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            business_id = int(request.data.get("business_id"))
        except (TypeError, ValueError):
            return Response(
                {"detail": "business_id is required (integer)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        plan_id = (request.data.get("plan_id") or "growth").strip().lower()
        if plan_id not in ("basic", "growth", "advanced"):
            plan_id = "growth"
        reason = (request.data.get("comp_reason") or "").strip()
        if not reason:
            return Response(
                {"detail": "comp_reason is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            business = BusinessInfo.objects.get(businessId=business_id)
        except BusinessInfo.DoesNotExist:
            return Response(
                {"detail": "Business not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        now = timezone.now()
        sub, _created = WidgetSubscription.objects.update_or_create(
            business=business,
            defaults={
                "plan_id": plan_id,
                "status": "active",
                "stripe_subscription_id": None,
                "comp_reason": reason,
                "payment_grace_until": None,
                "cancel_at_period_end": False,
                "current_period_end": None,
            },
        )
        try:
            AuditLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                user_email=getattr(request.user, "email", None) or "unknown",
                action="system_setting_change",
                details=f"Widget subscription comp override: plan={plan_id} reason={reason[:500]}",
                target_model="WidgetSubscription",
                target_id=str(sub.id),
                metadata={
                    "business_id": business.businessId,
                    "plan_id": plan_id,
                },
            )
        except Exception:
            pass
        return Response(
            {
                "id": str(sub.id),
                "business_id": business.businessId,
                "plan_id": sub.plan_id,
                "status": sub.status,
                "comp_reason": sub.comp_reason,
            },
            status=status.HTTP_200_OK,
        )
