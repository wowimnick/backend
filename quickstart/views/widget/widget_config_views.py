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
from quickstart.models import (
    BusinessInfo,
    BusinessAddonSubscription,
    ClassesMain,
    WidgetSubscription,
)
from quickstart.models import ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
from quickstart.serializers.widget.widget_config_serializer import (
    BusinessWidgetConfigSerializer,
    WidgetSubscriptionSerializer,
)
from quickstart.views.widget.widget_views import _business_has_active_widget_subscription

stripe.api_key = settings.STRIPE_SECRET_KEY

VALID_PLAN_IDS = {"basic", "growth", "advanced"}


def _subscription_client_secret_from_invoice(invoice):
    """Extract payment intent client_secret from a Stripe invoice (for subscription payment)."""
    def _pi_id(inv):
        if inv is None or isinstance(inv, str):
            return None
        pi = getattr(inv, "payment_intent", None) or (inv.get("payment_intent") if isinstance(inv, dict) else None)
        if pi is not None:
            return pi if isinstance(pi, str) else getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None)
        payments = getattr(inv, "payments", None) or (inv.get("payments") if isinstance(inv, dict) else None)
        if not payments:
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

    pi_id = _pi_id(invoice)
    if not pi_id:
        return None
    try:
        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
        return getattr(pi_obj, "client_secret", None) or (pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None)
    except stripe.StripeError:
        return None


def _get_subscription_client_secret(stripe_sub):
    """Get client_secret from a subscription's latest_invoice (for on-site payment)."""
    invoice = getattr(stripe_sub, "latest_invoice", None) or (stripe_sub.get("latest_invoice") if hasattr(stripe_sub, "get") else None)
    if invoice is not None and not isinstance(invoice, str):
        secret = _subscription_client_secret_from_invoice(invoice)
        if secret:
            return secret
    if stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(stripe_sub.id, expand=["latest_invoice.payments"])
            inv = getattr(expanded, "latest_invoice", None) or (expanded.get("latest_invoice") if hasattr(expanded, "get") else None)
            if inv is not None and not isinstance(inv, str):
                secret = _subscription_client_secret_from_invoice(inv)
                if secret:
                    return secret
        except stripe.StripeError:
            pass
    latest_invoice = getattr(stripe_sub, "latest_invoice", None) or (stripe_sub.get("latest_invoice") if hasattr(stripe_sub, "get") else None)
    invoice_id = getattr(latest_invoice, "id", None) if latest_invoice is not None and not isinstance(latest_invoice, str) else latest_invoice
    if invoice_id:
        try:
            invoice_obj = stripe.Invoice.retrieve(str(invoice_id), expand=["payments"])
            return _subscription_client_secret_from_invoice(invoice_obj)
        except stripe.StripeError:
            pass
    return None

# Define default domains that should always be allowed but hidden from the user UI.
DEFAULT_WIDGET_DOMAINS = {"classeasily.com", "staging.classeasily.com"}


def _widget_subscription_price_map():
    """Return dict of plan_id -> Stripe price id for widget subscription."""
    return {
        "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None),
        "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None),
        "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None),
    }


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
            "widget_email_branding": business.widget_email_branding or {},
            "marketplace_email_branding_enabled": getattr(
                business, "marketplace_email_branding_enabled", False
            ),
            "marketplace_email_branding": getattr(
                business, "marketplace_email_branding", None
            )
            or {},
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
        Expects a payload like: { "primary": "#ff385c", "fontFamily": "...", "allowed_widget_origins": "domain1\ndomain2", "widget_email_branding": {...} }
        widget_email_branding is only writable for Growth/Advanced widget plans.
        """
        business = self.get_business(request.user)

        data = dict(request.data)
        widget_email_branding = data.pop("widget_email_branding", None)

        if widget_email_branding is not None:
            sub = (
                WidgetSubscription.objects.filter(
                    business=business, status__in=["active", "trialing"]
                )
                .order_by("-current_period_end")
                .first()
            )
            plan_id = (sub.plan_id or "").lower() if sub else None
            if plan_id not in ("growth", "advanced"):
                return Response(
                    {
                        "detail": "Personalized booking emails (widget email branding) require a Growth or Advanced widget plan.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if isinstance(widget_email_branding, dict):
                business.widget_email_branding = widget_email_branding
                business.save(update_fields=["widget_email_branding"])

        # marketplace_email_branding_enabled is set only by addon subscription webhook; we do not accept it here.
        data.pop("marketplace_email_branding_enabled", None)
        marketplace_branding = data.pop("marketplace_email_branding", None)
        if marketplace_branding is not None and isinstance(marketplace_branding, dict):
            business.marketplace_email_branding = marketplace_branding
            business.save(update_fields=["marketplace_email_branding"])

        serializer = BusinessWidgetConfigSerializer(
            instance=business, data=data, partial=True
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


def _create_stripe_subscription_for_plan(business, plan_id, price_id):
    """
    Ensure Stripe customer, create a Stripe subscription (default_incomplete) for the plan,
    and return (stripe_sub, client_secret). Caller must link/update WidgetSubscription.
    """
    if not business.stripe_customer_id:
        customer = stripe.Customer.create(
            email=business.studentContactEmail,
            name=business.businessName,
            metadata={"business_id": str(business.businessId)},
        )
        business.stripe_customer_id = customer.id
        business.save(update_fields=["stripe_customer_id"])

    # Cancel any leftover incomplete subscriptions
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

    stripe_sub = stripe.Subscription.create(
        customer=business.stripe_customer_id,
        items=[{"price": price_id}],
        payment_behavior="default_incomplete",
        payment_settings={"save_default_payment_method": "on_subscription"},
        expand=["latest_invoice.payment_intent", "latest_invoice.payments"],
        metadata={
            "business_id": str(business.businessId),
            "plan_id": plan_id,
        },
    )

    client_secret = _client_secret_from_stripe_invoice(
        getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
    )
    if not client_secret and stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(
                stripe_sub.id,
                expand=["latest_invoice.payments"],
            )
            inv = getattr(expanded, "latest_invoice", None) or expanded.get("latest_invoice")
            client_secret = _client_secret_from_stripe_invoice(inv)
        except stripe.StripeError:
            pass
    if not client_secret:
        latest_inv = getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
        inv_id = getattr(latest_inv, "id", None) if latest_inv and not isinstance(latest_inv, str) else (latest_inv if isinstance(latest_inv, str) else None)
        if inv_id:
            try:
                inv_obj = stripe.Invoice.retrieve(str(inv_id), expand=["payments"])
                client_secret = _client_secret_from_stripe_invoice(inv_obj)
            except stripe.StripeError:
                pass
    return stripe_sub, client_secret


def _subscription_response_from_sub(sub):
    """Build subscription dict for API response from WidgetSubscription model."""
    return {
        "planId": sub.plan_id,
        "status": sub.status,
        "currentPeriodEnd": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
        "cancelAtPeriodEnd": sub.cancel_at_period_end,
    }


def _client_secret_from_stripe_invoice(invoice):
    """Extract payment_intent client_secret from a Stripe invoice (object or id)."""
    if invoice is None or isinstance(invoice, str):
        return None
    pi = getattr(invoice, "payment_intent", None) or (
        invoice.get("payment_intent") if isinstance(invoice, dict) else None
    )
    if pi is None:
        payments = getattr(invoice, "payments", None) or (
            invoice.get("payments") if isinstance(invoice, dict) else None
        )
        if payments:
            data = getattr(payments, "data", None) or payments.get("data")
            if data and len(data):
                first = data[0]
                payment = getattr(first, "payment", None) or (
                    first.get("payment") if isinstance(first, dict) else None
                )
                if payment:
                    pi = getattr(payment, "payment_intent", None) or (
                        payment.get("payment_intent") if isinstance(payment, dict) else None
                    )
    if pi is None:
        return None
    pi_id = pi if isinstance(pi, str) else (getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None))
    if not pi_id:
        return None
    try:
        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
        return getattr(pi_obj, "client_secret", None) or (
            pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None
        )
    except stripe.StripeError:
        return None


class WidgetSubscriptionView(APIView):
    """
    GET: Return current widget subscription for the authenticated business.
    POST: Create or update subscription with plan_id. All plan changes require successful payment;
    the new plan is only applied after payment (invoice.paid webhook syncs plan_id).
    - If business has an active subscription with stripe_subscription_id: updates the Stripe
      subscription to the new plan (prorated). If the new invoice requires payment (e.g. upgrade),
      returns requires_payment and client_secret; plan_id is not updated until payment succeeds.
    - If subscription exists but has no stripe_subscription_id: creates a Stripe subscription
      and returns requires_payment and client_secret; plan_id is set only after payment.
    - If no subscription: creates a Stripe subscription and returns requires_payment and
      client_secret; plan_id is set only after payment (invoice.paid).
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        subscription_required = getattr(settings, "WIDGET_SUBSCRIPTION_REQUIRED", False)
        if not sub:
            return Response(
                {
                    "subscription": None,
                    "widget_subscription_required": subscription_required,
                    "has_stripe_subscription": False,
                },
                status=status.HTTP_200_OK,
            )
        data = {
            "subscription": _subscription_response_from_sub(sub),
            "widget_subscription_required": subscription_required,
            "has_stripe_subscription": bool(sub.stripe_subscription_id),
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
        price_map = _widget_subscription_price_map()
        price_id = price_map.get(plan_id) or getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Widget subscription pricing is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        sub = _get_current_subscription(business)

        # --- Existing subscription: plan switch ---
        if sub:
            if sub.stripe_subscription_id:
                # Production path: update Stripe subscription (proration, payment if needed)
                try:
                    stripe_sub = stripe.Subscription.retrieve(
                        sub.stripe_subscription_id,
                        expand=["items.data.price"],
                    )
                except stripe.StripeError as e:
                    logger.warning("Stripe Subscription.retrieve failed: %s", e)
                    return Response(
                        {"error": "Could not load subscription. Please try again."},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
                items_data = stripe_sub.get("items") or {}
                item_list = (items_data.get("data") or []) if isinstance(items_data, dict) else []
                if not item_list:
                    return Response(
                        {"error": "Invalid subscription state. Please contact support."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                subscription_item_id = item_list[0].get("id")
                if not subscription_item_id:
                    return Response(
                        {"error": "Invalid subscription state. Please contact support."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # Same price = no-op
                current_price_id = (item_list[0].get("price") or {}).get("id") if item_list else None
                if current_price_id == price_id:
                    return Response(
                        {"subscription": _subscription_response_from_sub(sub)},
                        status=status.HTTP_200_OK,
                    )
                try:
                    stripe.Subscription.modify(
                        sub.stripe_subscription_id,
                        items=[{"id": subscription_item_id, "price": price_id}],
                        proration_behavior="create_prorations",
                        payment_behavior="pending_if_incomplete",
                        metadata={
                            "business_id": str(business.businessId),
                            "plan_id": plan_id,
                        },
                    )
                except stripe.StripeError as e:
                    logger.warning("Stripe Subscription.modify failed: %s", e)
                    return Response(
                        {"error": str(e)},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                # Re-retrieve with latest_invoice to see if payment is required
                try:
                    stripe_sub = stripe.Subscription.retrieve(
                        sub.stripe_subscription_id,
                        expand=["latest_invoice", "latest_invoice.payment_intent", "latest_invoice.payments"],
                    )
                except stripe.StripeError:
                    stripe_sub = stripe.Subscription.retrieve(sub.stripe_subscription_id)
                latest_invoice = getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
                client_secret = None
                if latest_invoice and not isinstance(latest_invoice, str):
                    client_secret = _client_secret_from_stripe_invoice(latest_invoice)
                if client_secret:
                    # Invoice requires payment (e.g. upgrade). Do not update plan_id yet; webhook will after payment.
                    return Response(
                        {
                            "requires_payment": True,
                            "client_secret": client_secret,
                            "subscription_id": sub.stripe_subscription_id,
                            "target_plan_id": plan_id,
                            "subscription": _subscription_response_from_sub(sub),
                        },
                        status=status.HTTP_200_OK,
                    )
                # No payment required (e.g. downgrade credit). Sync from Stripe.
                from datetime import datetime
                import pytz
                period_end = stripe_sub.get("current_period_end")
                current_period_end = (
                    datetime.fromtimestamp(period_end, tz=pytz.UTC) if period_end else None
                )
                sub.plan_id = plan_id
                sub.stripe_price_id = price_id
                sub.current_period_end = current_period_end
                sub.status = stripe_sub.get("status") or sub.status
                sub.cancel_at_period_end = bool(stripe_sub.get("cancel_at_period_end"))
                sub.save(
                    update_fields=[
                        "plan_id",
                        "stripe_price_id",
                        "current_period_end",
                        "status",
                        "cancel_at_period_end",
                    ]
                )
                return Response(
                    {
                        "subscription": _subscription_response_from_sub(sub),
                        "stripe_updated": True,
                    },
                    status=status.HTTP_200_OK,
                )
            # No Stripe subscription yet: create one and require payment (same as addons — always charge)
            try:
                stripe_sub, client_secret = _create_stripe_subscription_for_plan(
                    business, plan_id, price_id
                )
            except stripe.StripeError as e:
                logger.warning("Stripe subscription create (DB-only migrate) failed: %s", e)
                return Response(
                    {"error": str(e)},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            if not client_secret:
                return Response(
                    {"error": "Could not create payment form. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            stripe_price_id = None
            if stripe_sub.get("items") and stripe_sub["items"].get("data"):
                stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")
            # Attach new Stripe subscription to this existing row so webhook updates the same record.
            # Do not set plan_id here: subscription is only granted after payment (invoice.paid syncs plan_id).
            sub.stripe_subscription_id = stripe_sub.id
            sub.stripe_customer_id = business.stripe_customer_id
            sub.stripe_price_id = stripe_price_id
            sub.status = stripe_sub.get("status", "incomplete")
            sub.save(
                update_fields=[
                    "stripe_subscription_id",
                    "stripe_customer_id",
                    "stripe_price_id",
                    "status",
                ]
            )
            return Response(
                {
                    "requires_payment": True,
                    "client_secret": client_secret,
                    "subscription_id": stripe_sub.id,
                    "target_plan_id": plan_id,
                    "subscription": _subscription_response_from_sub(sub),
                },
                status=status.HTTP_200_OK,
            )

        # --- No subscription: create Stripe subscription and require payment (always charge) ---
        try:
            stripe_sub, client_secret = _create_stripe_subscription_for_plan(
                business, plan_id, price_id
            )
        except stripe.StripeError as e:
            logger.warning("Stripe subscription create (first-time) failed: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if not client_secret:
            return Response(
                {"error": "Could not create payment form. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        stripe_price_id = None
        if stripe_sub.get("items") and stripe_sub["items"].get("data"):
            stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")
        # Use placeholder plan_id until payment; invoice.paid webhook sets real plan_id from metadata.
        WidgetSubscription.objects.update_or_create(
            stripe_subscription_id=stripe_sub.id,
            defaults={
                "business": business,
                "stripe_customer_id": business.stripe_customer_id,
                "stripe_price_id": stripe_price_id,
                "status": stripe_sub.get("status", "incomplete"),
                "plan_id": "basic",
            },
        )
        sub = WidgetSubscription.objects.get(stripe_subscription_id=stripe_sub.id)
        return Response(
            {
                "requires_payment": True,
                "client_secret": client_secret,
                "subscription_id": stripe_sub.id,
                "target_plan_id": plan_id,
                "subscription": _subscription_response_from_sub(sub),
            },
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionCancelView(APIView):
    """POST: Set cancel_at_period_end=True for the current subscription. Syncs to Stripe when present."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if sub.stripe_subscription_id:
            try:
                stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
            except stripe.StripeError as e:
                logger.warning("Stripe Subscription.modify cancel_at_period_end failed: %s", e)
                return Response(
                    {"error": "Could not update cancellation. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end"])
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
    """POST: Set cancel_at_period_end=False for the current subscription. Syncs to Stripe when present."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if sub.stripe_subscription_id:
            try:
                stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
            except stripe.StripeError as e:
                logger.warning("Stripe Subscription.modify cancel_at_period_end=False failed: %s", e)
                return Response(
                    {"error": "Could not reactivate. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
        sub.cancel_at_period_end = False
        sub.save(update_fields=["cancel_at_period_end"])
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


class WidgetSubscriptionInvoicesView(APIView):
    """GET: List Stripe invoices for the business's widget subscription (paid/draft/open)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        # Prefer subscription with Stripe ID (any status) to list invoices
        sub = (
            WidgetSubscription.objects.filter(business=business)
            .exclude(stripe_subscription_id__isnull=True)
            .exclude(stripe_subscription_id="")
            .order_by("-created_at")
            .first()
        )
        if not sub or not sub.stripe_subscription_id:
            # Fallback: list by customer so we show something if they paid via Stripe
            customer_id = getattr(business, "stripe_customer_id", None)
            if not customer_id:
                return Response({"invoices": []}, status=status.HTTP_200_OK)
            try:
                stripe_invoices = stripe.Invoice.list(
                    customer=customer_id,
                    status="paid",
                    limit=50,
                )
            except stripe.StripeError as e:
                logger.warning("Stripe Invoice.list (customer) failed: %s", e)
                return Response({"invoices": []}, status=status.HTTP_200_OK)
        else:
            try:
                stripe_invoices = stripe.Invoice.list(
                    subscription=sub.stripe_subscription_id,
                    status="paid",
                    limit=50,
                )
            except stripe.StripeError as e:
                logger.warning("Stripe Invoice.list (subscription) failed: %s", e)
                return Response({"invoices": []}, status=status.HTTP_200_OK)

        invoices = []
        for inv in stripe_invoices.get("data", []):
            amount = (inv.get("amount_paid") or 0) / 100.0
            currency = (inv.get("currency") or "usd").upper()
            created = inv.get("created")
            if created:
                from datetime import datetime
                if isinstance(created, (int, float)):
                    created = datetime.utcfromtimestamp(created).isoformat() + "Z"
            invoices.append({
                "id": inv.get("id"),
                "number": inv.get("number") or inv.get("id"),
                "created": created,
                "amount_paid": amount,
                "currency": currency,
                "status": inv.get("status"),
                "invoice_pdf": inv.get("invoice_pdf"),
            })
        # Sort by created descending
        invoices.sort(key=lambda x: x.get("created") or "", reverse=True)
        return Response({"invoices": invoices}, status=status.HTTP_200_OK)


# ─── Addon subscriptions (e.g. marketplace email branding $7/mo) ─────────────────


def _get_current_addon_subscription(business, addon_type):
    now = timezone.now()
    return (
        BusinessAddonSubscription.objects.filter(
            business=business,
            addon_type=addon_type,
            status__in=["active", "trialing"],
            current_period_end__gt=now,
        )
        .order_by("-current_period_end")
        .first()
    )


def _normalize_payment_method_id(pm):
    """Extract payment method id from Stripe object (string or expanded)."""
    if pm is None:
        return None
    if isinstance(pm, str):
        return pm
    return getattr(pm, "id", None) or (pm.get("id") if isinstance(pm, dict) else None)


def _get_business_default_payment_method_id(business):
    """
    Return the business's default payment method id for charging, or None.
    Checks (1) Stripe customer invoice_settings.default_payment_method,
    (2) active widget subscription's default_payment_method (e.g. after widget checkout).
    """
    if not business.stripe_customer_id:
        return None
    try:
        customer = stripe.Customer.retrieve(
            business.stripe_customer_id,
            expand=["invoice_settings.default_payment_method"],
        )
        default_pm = getattr(
            getattr(customer, "invoice_settings", None),
            "default_payment_method",
            None,
        )
        pm_id = _normalize_payment_method_id(default_pm)
        if pm_id:
            return pm_id
    except stripe.StripeError:
        pass
    # Fallback: use payment method from active widget subscription (set when they paid for widget plan)
    try:
        now = timezone.now()
        widget_sub = (
            WidgetSubscription.objects.filter(
                business=business,
                status__in=["active", "trialing"],
                current_period_end__gt=now,
            )
            .order_by("-current_period_end")
            .first()
        )
        if widget_sub and widget_sub.stripe_subscription_id:
            stripe_sub = stripe.Subscription.retrieve(
                widget_sub.stripe_subscription_id,
                expand=["default_payment_method"],
            )
            default_pm = getattr(stripe_sub, "default_payment_method", None) or (
                stripe_sub.get("default_payment_method") if isinstance(stripe_sub, dict) else None
            )
            return _normalize_payment_method_id(default_pm)
    except stripe.StripeError:
        pass
    return None


def _business_can_instant_subscribe(business):
    """True if business has a Stripe customer with a default payment method (so we can charge without Checkout)."""
    return bool(_get_business_default_payment_method_id(business))


class BusinessAddonsView(APIView):
    """GET: Return status of addon subscriptions for the authenticated business."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        addon = _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
        # Only offer instant subscribe when not already subscribed and we have a saved payment method
        can_instant = (
            addon is None
            and _business_can_instant_subscribe(business)
        )
        data = {
            "marketplace_email_branding": {
                "active": addon is not None,
                "currentPeriodEnd": (
                    addon.current_period_end.isoformat() if addon and addon.current_period_end else None
                ),
                "cancelAtPeriodEnd": addon.cancel_at_period_end if addon else False,
                "canInstantSubscribe": can_instant,
            }
        }
        return Response(data, status=status.HTTP_200_OK)


class CreateMarketplaceEmailAddonCheckoutView(APIView):
    """POST: Create Stripe Checkout Session for marketplace email branding addon ($7/mo)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Marketplace email addon is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING):
            return Response(
                {"error": "You already have an active marketplace email branding subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        success_url = request.data.get(
            "success_url",
            request.build_absolute_uri("/business/dashboard/settings"),
        )
        cancel_url = request.data.get(
            "cancel_url",
            request.build_absolute_uri("/business/dashboard/settings"),
        )
        try:
            session_params = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url + "?addon=1&session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": cancel_url,
                "client_reference_id": str(business.businessId),
                "subscription_data": {
                    "metadata": {
                        "business_id": str(business.businessId),
                        "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
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
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class CreateMarketplaceEmailAddonPaymentIntentView(APIView):
    """
    POST: Create addon subscription in default_incomplete; return client_secret for on-site
    Stripe Elements payment. Payment happens on-site (no redirect).
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Marketplace email addon is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING):
            return Response(
                {"error": "You already have an active marketplace email branding subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            if not business.stripe_customer_id:
                customer = stripe.Customer.create(
                    email=business.studentContactEmail,
                    name=business.businessName,
                    metadata={"business_id": str(business.businessId)},
                )
                business.stripe_customer_id = customer.id
                business.save(update_fields=["stripe_customer_id"])

            incomplete = BusinessAddonSubscription.objects.filter(
                business=business,
                addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                status="incomplete",
            )
            for sub in incomplete:
                if sub.stripe_subscription_id:
                    try:
                        stripe.Subscription.cancel(sub.stripe_subscription_id)
                    except stripe.StripeError:
                        pass
            incomplete.delete()

            stripe_sub = stripe.Subscription.create(
                customer=business.stripe_customer_id,
                items=[{"price": price_id, "quantity": 1}],
                payment_behavior="default_incomplete",
                payment_settings={"save_default_payment_method": "on_subscription"},
                expand=["latest_invoice.payment_intent"],
                metadata={
                    "business_id": str(business.businessId),
                    "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                },
            )

            client_secret = _get_subscription_client_secret(stripe_sub)
            if not client_secret:
                logger.warning(
                    "addon payment-intent: no client_secret; sub_id=%s",
                    stripe_sub.id,
                )
                return Response(
                    {"error": "Could not create payment form. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            stripe_price_id = None
            if stripe_sub.get("items") and stripe_sub["items"].get("data"):
                stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")

            BusinessAddonSubscription.objects.update_or_create(
                stripe_subscription_id=stripe_sub.id,
                defaults={
                    "business": business,
                    "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                    "stripe_customer_id": business.stripe_customer_id or "",
                    "stripe_price_id": stripe_price_id,
                    "status": stripe_sub.status,
                },
            )

            return Response(
                {"client_secret": client_secret, "subscription_id": stripe_sub.id},
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class InstantSubscribeMarketplaceEmailAddonView(APIView):
    """
    POST: Subscribe to marketplace email branding addon using saved payment method.
    Only available when the business has a Stripe customer with a default payment method.
    Charges immediately; no redirect to Checkout. Addon is separate from widget plan.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Marketplace email addon is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING):
            return Response(
                {"error": "You already have an active marketplace email branding subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        pm_id = _get_business_default_payment_method_id(business)
        if not pm_id:
            return Response(
                {
                    "error": "No saved payment method. Use the link below to enter payment details.",
                    "can_instant": False,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            stripe_sub = stripe.Subscription.create(
                customer=business.stripe_customer_id,
                default_payment_method=pm_id,
                items=[{"price": price_id, "quantity": 1}],
                payment_behavior="error_if_incomplete",
                metadata={
                    "business_id": str(business.businessId),
                    "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                },
            )
        except stripe.StripeError as e:
            err_msg = str(e).lower()
            if "decline" in err_msg or "card" in err_msg or "payment" in err_msg:
                return Response(
                    {
                        "error": "Your saved card was declined. Use the link below to pay with a new card.",
                        "can_instant": False,
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )
            return Response(
                {"error": str(e), "can_instant": False},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Sync locally so UI updates immediately (webhook will also run)
        period_end = stripe_sub.current_period_end
        current_period_end = None
        if period_end:
            from datetime import datetime
            import pytz
            current_period_end = datetime.fromtimestamp(period_end, tz=pytz.UTC)
        stripe_price_id = None
        if stripe_sub.get("items") and stripe_sub["items"].get("data"):
            stripe_price_id = stripe_sub["items"]["data"][0].get("price", {}).get("id")

        BusinessAddonSubscription.objects.update_or_create(
            stripe_subscription_id=stripe_sub.id,
            defaults={
                "business": business,
                "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                "stripe_customer_id": stripe_sub.get("customer") or "",
                "stripe_price_id": stripe_price_id,
                "status": stripe_sub.status,
                "current_period_end": current_period_end,
                "cancel_at_period_end": bool(stripe_sub.get("cancel_at_period_end")),
            },
        )
        business.marketplace_email_branding_enabled = stripe_sub.status in ("active", "trialing")
        business.save(update_fields=["marketplace_email_branding_enabled"])

        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        current_period_end.isoformat() if current_period_end else None
                    ),
                    "cancelAtPeriodEnd": False,
                }
            },
            status=status.HTTP_200_OK,
        )


class CancelMarketplaceEmailAddonView(APIView):
    """POST: Set cancel_at_period_end=True for the marketplace email addon subscription."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
        if not sub or not sub.stripe_subscription_id:
            return Response(
                {"error": "No active addon subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        except stripe.StripeError as e:
            logger.warning("Stripe modify cancel_at_period_end failed: %s", e)
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end"])
        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": True,
                }
            },
            status=status.HTTP_200_OK,
        )


class ReactivateMarketplaceEmailAddonView(APIView):
    """POST: Set cancel_at_period_end=False for the marketplace email addon subscription."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
        if not sub or not sub.stripe_subscription_id:
            return Response(
                {"error": "No active addon subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
        except stripe.StripeError as e:
            logger.warning("Stripe modify cancel_at_period_end failed: %s", e)
        sub.cancel_at_period_end = False
        sub.save(update_fields=["cancel_at_period_end"])
        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": False,
                }
            },
            status=status.HTTP_200_OK,
        )
