# quickstart/views/business/widget_config_views.py

import logging
import stripe
from django.conf import settings

logger = logging.getLogger(__name__)
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from quickstart.utils.permissions import CanManageOwnClasses
from quickstart.models import BusinessInfo, ClassesMain, WidgetSubscription
from quickstart.serializers.widget.widget_config_serializer import (
    BusinessWidgetConfigSerializer,
    WidgetSubscriptionSerializer,
)
from quickstart.views.widget.widget_views import _business_has_active_widget_subscription

stripe.api_key = settings.STRIPE_SECRET_KEY

VALID_PLAN_IDS = {"basic", "growth", "advanced"}

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
                "planId": sub.plan_id,
                "status": sub.status,
                "currentPeriodEnd": (
                    sub.current_period_end.isoformat()
                    if sub.current_period_end
                    else None
                ),
                "cancelAtPeriodEnd": sub.cancel_at_period_end,
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
    Creates a Stripe Checkout Session for the widget subscription (Basic/Growth/Advanced).
    Accepts plan_id and returns the session URL for the frontend to redirect the user to Stripe.
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
        plan_id = (request.data.get("plan_id") or "growth").strip().lower()
        if plan_id not in VALID_PLAN_IDS:
            plan_id = "growth"
        price_map = {
            "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None),
            "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None),
            "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None),
        }
        price_id = price_map.get(plan_id) or getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Widget subscription is not configured. Please set Stripe Price IDs in settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = self.get_business(request.user)
        success_url = request.data.get(
            "success_url",
            request.build_absolute_uri("/business/dashboard/widget"),
        )
        cancel_url = request.data.get(
            "cancel_url",
            request.build_absolute_uri("/booking-widget/checkout"),
        )
        try:
            session_params = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url + "?session_id={CHECKOUT_SESSION_ID}&subscribed=1",
                "cancel_url": cancel_url + ("?" if "?" not in cancel_url else "&") + f"plan={plan_id}",
                "client_reference_id": str(business.businessId),
                "subscription_data": {
                    "metadata": {
                        "business_id": str(business.businessId),
                        "plan_id": plan_id,
                    },
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


class CreateWidgetSubscriptionPaymentIntentView(APIView):
    """
    Creates a Stripe Subscription in default_incomplete mode for inline Stripe Elements checkout.
    Returns { client_secret, subscription_id } so the frontend can confirm payment inline.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def _get_business(self, user):
        business = BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            from rest_framework.exceptions import NotFound
            raise NotFound("You are not a member of any business.")
        return business

    def post(self, request, *args, **kwargs):
        plan_id = (request.data.get("plan_id") or "growth").strip().lower()
        if plan_id not in VALID_PLAN_IDS:
            plan_id = "growth"

        price_map = {
            "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None),
            "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None),
            "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None),
        }
        price_id = price_map.get(plan_id) or getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Widget subscription is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        business = self._get_business(request.user)

        # Block if already active
        now = timezone.now()
        active_sub = WidgetSubscription.objects.filter(
            business=business,
            status__in=["active", "trialing"],
            current_period_end__gt=now,
        ).first()
        if active_sub:
            return Response(
                {"error": "already_subscribed", "plan_id": active_sub.plan_id},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            # Ensure Stripe customer exists
            if not business.stripe_customer_id:
                customer = stripe.Customer.create(
                    email=business.studentContactEmail,
                    name=business.businessName,
                    metadata={"business_id": str(business.businessId)},
                )
                business.stripe_customer_id = customer.id
                business.save(update_fields=["stripe_customer_id"])

            # Cancel any leftover incomplete subscriptions to avoid stale intents
            incomplete_subs = WidgetSubscription.objects.filter(
                business=business,
                status="incomplete",
            )
            for sub in incomplete_subs:
                if sub.stripe_subscription_id:
                    try:
                        stripe.Subscription.cancel(sub.stripe_subscription_id)
                    except stripe.StripeError:
                        pass
            incomplete_subs.delete()

            # Create subscription in incomplete mode so we get a payment intent
            stripe_sub = stripe.Subscription.create(
                customer=business.stripe_customer_id,
                items=[{"price": price_id}],
                payment_behavior="default_incomplete",
                payment_settings={"save_default_payment_method": "on_subscription"},
                expand=["latest_invoice.payment_intent"],
                metadata={
                    "business_id": str(business.businessId),
                    "plan_id": plan_id,
                },
            )

            # Safely extract client_secret. Payment intent can be an object or an id string.
            # Create response sometimes does not return expanded nested objects; re-retrieve if needed.
            def _client_secret_from_pi(pi):
                if pi is None:
                    return None
                # Object with client_secret
                if hasattr(pi, "client_secret") and pi.client_secret:
                    return pi.client_secret
                # Dict-style (StripeObject can be accessed via get)
                if isinstance(pi, dict) and pi.get("client_secret"):
                    return pi["client_secret"]
                # payment_intent may be an id string when not expanded
                pi_id = getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None) or (pi if isinstance(pi, str) else None)
                if pi_id:
                    try:
                        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
                        return getattr(pi_obj, "client_secret", None) or (pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None)
                    except stripe.StripeError as e:
                        logger.warning("PaymentIntent.retrieve failed for %s: %s", pi_id, e)
                return None

            # In current Stripe API, Invoice has no top-level payment_intent; it's under
            # invoice.payments.data[].payment.payment_intent (Invoice Payment object).
            def _pi_id_from_invoice(invoice):
                if invoice is None or isinstance(invoice, str):
                    return None
                # Legacy: top-level payment_intent (older API or expand)
                pi = getattr(invoice, "payment_intent", None) or (invoice.get("payment_intent") if isinstance(invoice, dict) else None)
                if pi is not None:
                    return pi if isinstance(pi, str) else getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None)
                # New API: invoice.payments.data[0].payment.payment_intent
                payments = getattr(invoice, "payments", None) or (invoice.get("payments") if isinstance(invoice, dict) else None)
                if payments is None:
                    return None
                data = getattr(payments, "data", None) or (payments.get("data") if isinstance(payments, dict) else None)
                if not data or not len(data):
                    return None
                first = data[0] if hasattr(data, "__getitem__") else next(iter(data), None)
                if first is None:
                    return None
                payment = getattr(first, "payment", None) or (first.get("payment") if isinstance(first, dict) else None)
                if payment is None:
                    return None
                pi_id = getattr(payment, "payment_intent", None) or (payment.get("payment_intent") if isinstance(payment, dict) else None)
                return pi_id if isinstance(pi_id, str) else (getattr(pi_id, "id", None) or (pi_id.get("id") if isinstance(pi_id, dict) else None))

            def _client_secret_from_invoice(invoice):
                pi_id = _pi_id_from_invoice(invoice)
                return _client_secret_from_pi(pi_id) if pi_id else _client_secret_from_pi(None)

            client_secret = None
            # 1) From create response (may have no payments expanded)
            invoice = getattr(stripe_sub, "latest_invoice", None) or (stripe_sub.get("latest_invoice") if hasattr(stripe_sub, "get") else None)
            if invoice is not None and not isinstance(invoice, str):
                client_secret = _client_secret_from_invoice(invoice)

            # 2) Re-retrieve subscription with expand (create often omits nested expand)
            if not client_secret and stripe_sub.id:
                try:
                    stripe_sub_expanded = stripe.Subscription.retrieve(
                        stripe_sub.id,
                        expand=["latest_invoice.payments"],
                    )
                    inv = getattr(stripe_sub_expanded, "latest_invoice", None)
                    if inv is None and hasattr(stripe_sub_expanded, "get"):
                        inv = stripe_sub_expanded.get("latest_invoice")
                    if inv is not None and not isinstance(inv, str):
                        client_secret = _client_secret_from_invoice(inv)
                except stripe.StripeError as e:
                    logger.warning("Subscription.retrieve expand failed: %s", e)

            # 3) Fallback: get invoice id and retrieve invoice with payments expanded
            if not client_secret:
                latest_invoice = getattr(stripe_sub, "latest_invoice", None) or (stripe_sub.get("latest_invoice") if hasattr(stripe_sub, "get") else None)
                invoice_id = getattr(latest_invoice, "id", None) if latest_invoice is not None and not isinstance(latest_invoice, str) else latest_invoice
                if invoice_id:
                    try:
                        invoice_obj = stripe.Invoice.retrieve(
                            str(invoice_id), expand=["payments"]
                        )
                        client_secret = _client_secret_from_invoice(invoice_obj)
                    except stripe.StripeError as e:
                        logger.warning("Invoice.retrieve failed for %s: %s", invoice_id, e)

            if not client_secret:
                logger.warning(
                    "widget-subscription payment-intent: no client_secret; sub_id=%s",
                    stripe_sub.id,
                )
                return Response(
                    {"error": "Could not create payment intent. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            # Pre-create a pending WidgetSubscription record so the webhook can update it
            stripe_price_id = None
            if stripe_sub.get("items") and stripe_sub["items"].get("data"):
                stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")

            WidgetSubscription.objects.update_or_create(
                stripe_subscription_id=stripe_sub.id,
                defaults={
                    "business": business,
                    "stripe_customer_id": business.stripe_customer_id,
                    "stripe_price_id": stripe_price_id,
                    "status": stripe_sub.status,
                    "plan_id": plan_id,
                },
            )

            return Response(
                {
                    "client_secret": client_secret,
                    "subscription_id": stripe_sub.id,
                },
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


def _get_business_for_subscription(user):
    """Get the business for the current user (owner or accepted staff)."""
    business = BusinessInfo.objects.filter(
        Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
    ).first()
    if not business:
        from rest_framework.exceptions import NotFound
        raise NotFound("You are not a member of any business.")
    return business


def _get_current_subscription(business):
    """Get the current active/trialing subscription for the business, or None."""
    now = timezone.now()
    return (
        WidgetSubscription.objects.filter(
            business=business,
            status__in=["active", "trialing"],
            current_period_end__gt=now,
        )
        .order_by("-current_period_end")
        .first()
    )


class WidgetSubscriptionView(APIView):
    """
    GET: Return current widget subscription for the authenticated business.
    POST: Create or update subscription with plan_id (direct, no Stripe). Sets current_period_end to now + 1 month.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response({"subscription": None}, status=status.HTTP_200_OK)
        data = {
            "subscription": {
                "planId": sub.plan_id,
                "status": sub.status,
                "currentPeriodEnd": (
                    sub.current_period_end.isoformat() if sub.current_period_end else None
                ),
                "cancelAtPeriodEnd": sub.cancel_at_period_end,
            }
        }
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        plan_id = (request.data.get("plan_id") or "").strip().lower()
        if plan_id not in VALID_PLAN_IDS:
            return Response(
                {"error": "Invalid plan_id. Must be one of: basic, growth, advanced."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        business = _get_business_for_subscription(request.user)
        now = timezone.now()
        period_end = now + timedelta(days=31)

        sub = _get_current_subscription(business)
        if sub:
            sub.plan_id = plan_id
            sub.current_period_end = period_end
            sub.cancel_at_period_end = False
            sub.status = "active"
            sub.save()
        else:
            sub = WidgetSubscription.objects.create(
                business=business,
                plan_id=plan_id,
                status="active",
                current_period_end=period_end,
                cancel_at_period_end=False,
            )
        return Response(
            {
                "subscription": {
                    "planId": sub.plan_id,
                    "status": sub.status,
                    "currentPeriodEnd": sub.current_period_end.isoformat(),
                    "cancelAtPeriodEnd": sub.cancel_at_period_end,
                }
            },
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionCancelView(APIView):
    """POST: Set cancel_at_period_end=True for the current subscription."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        sub.cancel_at_period_end = True
        sub.save()
        return Response(
            {
                "subscription": {
                    "planId": sub.plan_id,
                    "status": sub.status,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": True,
                }
            },
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionReactivateView(APIView):
    """POST: Set cancel_at_period_end=False for the current subscription."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        sub.cancel_at_period_end = False
        sub.save()
        return Response(
            {
                "subscription": {
                    "planId": sub.plan_id,
                    "status": sub.status,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": False,
                }
            },
            status=status.HTTP_200_OK,
        )
