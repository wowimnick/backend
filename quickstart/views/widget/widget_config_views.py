# quickstart/views/business/widget_config_views.py

import stripe
from django.conf import settings
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from quickstart.utils.permissions import CanManageOwnClasses
from quickstart.models import BusinessInfo, ClassesMain, WidgetSubscription
from quickstart.serializers.widget.widget_config_serializer import (
    BusinessWidgetConfigSerializer,
)
from quickstart.views.widget.widget_views import _business_has_active_widget_subscription

stripe.api_key = settings.STRIPE_SECRET_KEY

# Define default domains that should always be allowed but hidden from the user UI.
DEFAULT_WIDGET_DOMAINS = {"classeasily.com", "staging.classeasily.com"}


class WidgetConfigManagementView(APIView):
    """
    Manages the widget configuration for the authenticated user's business.
    GET: Retrieves the current config and associated classes.
    PATCH: Updates the widget configuration.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get_business(self, user):
        """Helper to get the business profile for the current user (owner or accepted staff)."""
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            from rest_framework.exceptions import NotFound
            raise NotFound("You are not a member of any business.")
        return business

    def get(self, request, *args, **kwargs):
        """
        Returns the widget config, a list of classes, and the widget API key for the business.
        """
        business = self.get_business(request.user)

        # Fetch active classes for the "Feature a Specific Class" dropdown
        classes = ClassesMain.objects.filter(
            businessId=business, status="active"
        ).values("classId", "title")

        # Combine widget config and allowed origins into a single config object for the frontend
        config_data = business.widget_config or {}

        # Get user-configured domains by filtering out the default ones for display.
        user_configured_origins = [
            origin
            for origin in (business.allowed_widget_origins or [])
            if origin not in DEFAULT_WIDGET_DOMAINS
        ]
        config_data["allowed_widget_origins"] = "\n".join(user_configured_origins)

        response_data = {
            "classes": list(classes),
            "config": config_data,
            "widget_api_key": business.widget_api_key,
            "has_active_widget_subscription": _business_has_active_widget_subscription(
                business
            ),
        }
        sub = (
            WidgetSubscription.objects.filter(
                business=business, status__in=["active", "trialing"]
            )
            .order_by("-current_period_end")
            .first()
        )
        if sub:
            response_data["widget_subscription"] = {
                "status": sub.status,
                "current_period_end": (
                    sub.current_period_end.isoformat()
                    if sub.current_period_end
                    else None
                ),
            }
        else:
            response_data["widget_subscription"] = None
        return Response(response_data, status=status.HTTP_200_OK)

    def patch(self, request, *args, **kwargs):
        """
        Updates the widget config for the business.
        Expects a payload like: { "primary": "#ff385c", "fontFamily": "...", "allowed_widget_origins": "domain1\ndomain2" }
        """
        business = self.get_business(request.user)

        serializer = BusinessWidgetConfigSerializer(
            instance=business, data=request.data, partial=True
        )

        if serializer.is_valid(raise_exception=True):
            serializer.save()
            return Response(
                {"message": "Widget configuration updated successfully."},
                status=status.HTTP_200_OK,
            )

        # This line is technically not needed due to raise_exception=True, but serves as a fallback.
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CreateWidgetSubscriptionCheckoutView(APIView):
    """
    Creates a Stripe Checkout Session for the $50/month widget subscription.
    Returns the session URL for the frontend to redirect the user.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get_business(self, user):
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            from rest_framework.exceptions import NotFound
            raise NotFound("You are not a member of any business.")
        return business

    def post(self, request, *args, **kwargs):
        price_id = getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Widget subscription is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = self.get_business(request.user)
        success_url = request.data.get(
            "success_url",
            request.build_absolute_uri("/business/dashboard/widget"),
        )
        cancel_url = request.data.get(
            "cancel_url",
            request.build_absolute_uri("/business/dashboard/widget"),
        )
        try:
            session_params = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": cancel_url,
                "client_reference_id": str(business.businessId),
                "subscription_data": {
                    "metadata": {"business_id": str(business.businessId)},
                },
            }
            if business.stripe_customer_id:
                session_params["customer"] = business.stripe_customer_id
            else:
                session_params["customer_email"] = business.studentContactEmail
            session = stripe.checkout.Session.create(**session_params)
            return Response({"url": session.url}, status=status.HTTP_200_OK)
        except stripe.StripeError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
