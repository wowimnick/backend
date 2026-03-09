# quickstart/views/business/widget_config_views.py

import logging
import stripe
from django.conf import settings

logger = logging.getLogger(__name__)
from django.db import transaction
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
from quickstart.services.subscription_sync import (
    sync_widget_subscription_from_stripe,
    sync_addon_subscription_from_stripe,
    mark_widget_subscription_canceled,
    mark_addon_subscription_canceled,
    _widget_price_to_plan_id,
)

stripe.api_key = settings.STRIPE_SECRET_KEY

# Plan order for upgrade/downgrade: lower index = lower tier
PLAN_ORDER = ["basic", "growth", "advanced"]


def _is_downgrade(from_plan_id, to_plan_id):
    if from_plan_id not in VALID_PLAN_IDS or to_plan_id not in VALID_PLAN_IDS:
        return False
    return PLAN_ORDER.index(to_plan_id) < PLAN_ORDER.index(from_plan_id)

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

        # Pin widget to a specific class (specificClassId) is Growth/Advanced only.
        sub = (
            WidgetSubscription.objects.filter(
                business=business, status__in=["active", "trialing"]
            )
            .order_by("-current_period_end")
            .first()
        )
        plan_id = (sub.plan_id or "").lower() if sub else None
        if plan_id not in ("growth", "advanced"):
            if data.get("specificClassId"):
                return Response(
                    {
                        "detail": "Pin widget to a specific class requires a Growth or Advanced widget plan.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            data.pop("specificClassId", None)

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
                # Invoice may already be paid (e.g. default payment method charged immediately)
                try:
                    stripe_sub_fresh = stripe.Subscription.retrieve(stripe_sub.id)
                    if (stripe_sub_fresh.get("status") or "").strip().lower() in ("active", "trialing"):
                        synced, _ = sync_widget_subscription_from_stripe(
                            stripe_sub.id, subscription_obj=stripe_sub_fresh
                        )
                        if synced:
                            return Response(
                                {
                                    "subscription_id": stripe_sub.id,
                                    "already_active": True,
                                    "subscription": _subscription_response_from_sub(synced),
                                },
                                status=status.HTTP_200_OK,
                            )
                except stripe.StripeError:
                    pass
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


def _get_latest_widget_subscription_with_stripe(business):
    """Get the most recent WidgetSubscription for this business that has a Stripe id (for sync-from-Stripe recovery).
    Order by -created_at so we sync the subscription the user just created, not an older one."""
    return (
        WidgetSubscription.objects.filter(
            business=business,
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .order_by("-created_at")
        .first()
    )


def _get_best_widget_subscription_for_display(business):
    """For GET recovery: prefer an active/trialing subscription when multiple exist (e.g. one incomplete_expired, one active).
    Returns (synced WidgetSubscription or None, error). Syncs from Stripe; prefers first that is active/trialing."""
    candidates = (
        WidgetSubscription.objects.filter(
            business=business,
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .order_by("-created_at")
    )
    best = None
    for row in candidates:
        synced, _ = sync_widget_subscription_from_stripe(row.stripe_subscription_id)
        if synced and (synced.status or "").strip().lower() in ("active", "trialing"):
            return synced, None
        if synced and best is None:
            best = synced
    return best, None


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
    plan_id = (sub.plan_id or "").strip().lower() or None
    if not plan_id and (sub.status or "").strip().lower() in ("active", "trialing"):
        plan_id = "basic"
    return {
        "planId": plan_id,
        "status": sub.status,
        "currentPeriodEnd": (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        ),
        "cancelAtPeriodEnd": sub.cancel_at_period_end,
    }


def _client_secret_from_stripe_invoice(invoice):
    """Extract payment_intent client_secret from a Stripe invoice (object or id)."""
    if invoice is None or isinstance(invoice, str):
        logger.info(
            "widget_subscription: _client_secret_from_stripe_invoice invoice is None or str id=%s",
            invoice if isinstance(invoice, str) else "None",
        )
        return None
    inv_id = getattr(invoice, "id", None) or (invoice.get("id") if isinstance(invoice, dict) else None)
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
        logger.info(
            "widget_subscription: _client_secret_from_stripe_invoice no payment_intent on invoice inv_id=%s",
            inv_id,
        )
        return None
    pi_id = pi if isinstance(pi, str) else (getattr(pi, "id", None) or (pi.get("id") if isinstance(pi, dict) else None))
    if not pi_id:
        logger.info("widget_subscription: _client_secret_from_stripe_invoice no pi_id from invoice inv_id=%s", inv_id)
        return None
    try:
        pi_obj = stripe.PaymentIntent.retrieve(pi_id)
        pi_status = getattr(pi_obj, "status", None) or (
            pi_obj.get("status") if isinstance(pi_obj, dict) else None
        )
        # Only return client_secret for PIs that can be used with Elements (non-terminal).
        if pi_status not in ("requires_payment_method", "requires_confirmation", "requires_action"):
            logger.info(
                "widget_subscription: _client_secret_from_stripe_invoice PI not usable for Elements pi_id=%s status=%s",
                pi_id,
                pi_status,
            )
            return None
        secret = getattr(pi_obj, "client_secret", None) or (
            pi_obj.get("client_secret") if isinstance(pi_obj, dict) else None
        )
        if secret:
            logger.info(
                "widget_subscription: _client_secret_from_stripe_invoice returning client_secret for pi_id=%s status=%s",
                pi_id,
                pi_status,
            )
        return secret
    except stripe.StripeError as e:
        logger.warning(
            "widget_subscription: _client_secret_from_stripe_invoice PI retrieve failed pi_id=%s err=%s",
            pi_id,
            e,
        )
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
        # Single source of truth: sync from Stripe when we have a linked subscription
        if sub and sub.stripe_subscription_id:
            synced, err = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
            if synced:
                sub = synced
        else:
            # No "current" sub found. Recover from Stripe: prefer active/trialing when multiple exist
            # (e.g. one incomplete_expired from abandoned upgrade, one active from new purchase).
            sub, _ = _get_best_widget_subscription_for_display(business)
            if not sub:
                sub = _get_current_subscription(business)
        subscription_required = getattr(settings, "WIDGET_SUBSCRIPTION_REQUIRED", False)
        has_widget_access = _business_has_active_widget_subscription(business)
        if not sub:
            return Response(
                {
                    "subscription": None,
                    "widget_subscription_required": subscription_required,
                    "has_stripe_subscription": False,
                    "has_widget_access": has_widget_access,
                },
                status=status.HTTP_200_OK,
            )
        payload = {
            "subscription": _subscription_response_from_sub(sub),
            "widget_subscription_required": subscription_required,
            "has_stripe_subscription": bool(sub.stripe_subscription_id),
            "has_widget_access": has_widget_access,
        }
        # Optional: scheduled downgrade from Stripe subscription schedule
        if sub.stripe_subscription_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(
                    sub.stripe_subscription_id,
                    expand=["schedule"],
                )
                sched = stripe_sub.get("schedule")
                schedule_id = None
                if isinstance(sched, str):
                    schedule_id = sched
                elif sched is not None:
                    schedule_id = sched.get("id") if isinstance(sched, dict) else getattr(sched, "id", None)
                if schedule_id:
                    schedule = stripe.SubscriptionSchedule.retrieve(schedule_id, expand=["phases"])
                    phases = getattr(schedule, "phases", None) or schedule.get("phases") or []
                    if len(phases) >= 2:
                        next_phase = phases[1]
                        next_items = (next_phase.get("items") or []) if isinstance(next_phase, dict) else getattr(next_phase, "items", []) or []
                        if next_items:
                            next_price_id = next_items[0].get("price") if isinstance(next_items[0], dict) else getattr(next_items[0], "price", None)
                            if next_price_id and isinstance(next_price_id, str) is False:
                                next_price_id = next_price_id.get("id") if isinstance(next_price_id, dict) else getattr(next_price_id, "id", None)
                            scheduled_plan_id = _widget_price_to_plan_id(next_price_id)
                            start = next_phase.get("start_date") if isinstance(next_phase, dict) else getattr(next_phase, "start_date", None)
                            if scheduled_plan_id and scheduled_plan_id != (sub.plan_id or "").strip().lower() and start:
                                from datetime import datetime as dt
                                payload["scheduled_downgrade"] = {
                                    "planId": scheduled_plan_id,
                                    "effectiveDate": dt.utcfromtimestamp(start).isoformat() + "Z" if isinstance(start, (int, float)) else str(start),
                                }
            except stripe.StripeError:
                pass
        return Response(payload, status=status.HTTP_200_OK)

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
        # If no "current" sub (e.g. current_period_end null or filter missed), use latest with Stripe id so we modify instead of creating a duplicate.
        if not sub:
            fallback = _get_latest_widget_subscription_with_stripe(business)
            if fallback and fallback.stripe_subscription_id:
                try:
                    stripe_sub_check = stripe.Subscription.retrieve(fallback.stripe_subscription_id)
                    if (stripe_sub_check.get("status") or "").strip().lower() in ("active", "trialing"):
                        sub = fallback
                except stripe.StripeError:
                    pass

        with transaction.atomic():
            # Lock so concurrent plan-switch POSTs for the same business serialize (avoid race).
            if sub:
                sub = WidgetSubscription.objects.select_for_update().get(pk=sub.pk)
            else:
                BusinessInfo.objects.select_for_update().get(pk=business.pk)

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
                    # Stripe already has this price (e.g. plan was applied by webhook or previous payment; our DB was stale).
                    # If user reloaded after clicking switch but before paying, there may be an open invoice — require payment, don't grant plan.
                    current_price_id = (item_list[0].get("price") or {}).get("id") if item_list else None
                    if current_price_id == price_id:
                        open_for_same = []
                        try:
                            open_for_same = stripe.Invoice.list(
                                subscription=sub.stripe_subscription_id,
                                status="open",
                                limit=1,
                            ).get("data") or []
                        except stripe.StripeError as e:
                            logger.warning("widget_subscription: open invoice check (already same price) failed: %s", e)
                        if open_for_same:
                            # Reload-after-switch: open invoice exists; return requires_payment so they can complete payment.
                            client_secret = _client_secret_from_stripe_invoice(open_for_same[0])
                            if client_secret:
                                logger.info(
                                    "widget_subscription: Stripe already has target price but open invoice; returning requires_payment sub_id=%s",
                                    sub.stripe_subscription_id,
                                )
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
                            return Response(
                                {"error": "Payment is required to complete this plan change. Please complete payment when prompted or try again."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        logger.info(
                            "widget_subscription: Stripe already has target price; syncing DB sub_id=%s plan_id=%s",
                            sub.stripe_subscription_id,
                            plan_id,
                        )
                        from datetime import datetime
                        import pytz
                        period_end = stripe_sub.get("current_period_end")
                        current_period_end = (
                            datetime.fromtimestamp(period_end, tz=pytz.UTC) if period_end else None
                        )
                        # Only use Stripe status if it keeps the sub visible to GET (active/trialing).
                        # GET uses status__in=["active", "trialing"] and current_period_end__gt=now.
                        stripe_status = (stripe_sub.get("status") or "").strip().lower()
                        if stripe_status in ("active", "trialing"):
                            sub.status = stripe_status
                        # If Stripe's period_end is in the past, keep existing so GET still finds this sub.
                        now = timezone.now()
                        if current_period_end is not None and current_period_end <= now:
                            current_period_end = sub.current_period_end  # keep existing
                        sub.plan_id = plan_id
                        sub.stripe_price_id = price_id
                        if current_period_end is not None:
                            sub.current_period_end = current_period_end
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
                    current_plan_id = _widget_price_to_plan_id(current_price_id) or (sub.plan_id or "").strip().lower() or "growth"
                    is_downgrade = _is_downgrade(current_plan_id, plan_id)

                    if is_downgrade:
                        # Downgrade at end of billing period: use Subscription Schedule (no immediate charge).
                        period_end_ts = stripe_sub.get("current_period_end")
                        if not period_end_ts:
                            return Response(
                                {"error": "Could not schedule downgrade. Please try again."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        try:
                            existing_schedule = (stripe_sub.get("schedule") or "") if isinstance(stripe_sub.get("schedule"), str) else (getattr(stripe_sub.get("schedule"), "id", None) if stripe_sub.get("schedule") else None)
                            if existing_schedule:
                                schedule = stripe.SubscriptionSchedule.retrieve(existing_schedule, expand=["phases"])
                                phases = getattr(schedule, "phases", None) or schedule.get("phases") or []
                                if len(phases) >= 2:
                                    return Response(
                                        {"error": "A plan change is already scheduled. It will take effect at the end of your billing period."},
                                        status=status.HTTP_400_BAD_REQUEST,
                                    )
                            schedule = stripe.SubscriptionSchedule.create(
                                from_subscription=sub.stripe_subscription_id,
                            )
                            stripe.SubscriptionSchedule.update(
                                schedule.id,
                                phases=[
                                    {
                                        "items": [{"price": current_price_id}],
                                        "end_date": period_end_ts,
                                    },
                                    {
                                        "items": [{"price": price_id}],
                                        "proration_behavior": "none",
                                    },
                                ],
                                metadata={"business_id": str(business.businessId), "plan_id": plan_id},
                            )
                        except stripe.StripeError as e:
                            logger.warning("widget_subscription: SubscriptionSchedule create/update failed sub_id=%s err=%s", sub.stripe_subscription_id, e)
                            return Response(
                                {"error": str(e) if str(e) else "Could not schedule downgrade. Please try again."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
                        sub = synced or sub
                        return Response(
                            {
                                "subscription": _subscription_response_from_sub(sub),
                                "stripe_updated": True,
                                "downgrade_scheduled_at_period_end": True,
                                "scheduled_plan_id": plan_id,
                            },
                            status=status.HTTP_200_OK,
                        )

                    logger.info(
                        "widget_subscription: plan switch modify (upgrade) sub_id=%s current_price=%s target_price=%s plan_id=%s",
                        sub.stripe_subscription_id,
                        current_price_id,
                        price_id,
                        plan_id,
                    )
                    # Use customer's saved payment method for the upgrade invoice so Stripe can charge automatically.
                    pm_id = _get_business_default_payment_method_id(business)
                    modify_params = {
                        "items": [{"id": subscription_item_id, "price": price_id}],
                        "proration_behavior": "always_invoice",
                        "payment_behavior": "pending_if_incomplete",
                        "expand": ["latest_invoice", "latest_invoice.payment_intent", "latest_invoice.payments"],
                    }
                    if pm_id:
                        modify_params["default_payment_method"] = pm_id
                    try:
                        # Upgrade: immediate proration and invoice; Stripe attempts payment with default_payment_method when set.
                        stripe_sub = stripe.Subscription.modify(
                            sub.stripe_subscription_id,
                            **modify_params,
                        )
                    except stripe.StripeError as e:
                        logger.warning("widget_subscription: Stripe Subscription.modify failed sub_id=%s err=%s", sub.stripe_subscription_id, e)
                        return Response(
                            {"error": str(e)},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # Update subscription metadata so invoice.paid webhook syncs correct plan_id; clear cancel_at_period_end so upgrade/downgrade doesn't show "Reactivate".
                    try:
                        stripe.Subscription.modify(
                            sub.stripe_subscription_id,
                            metadata={"business_id": str(business.businessId), "plan_id": plan_id},
                            cancel_at_period_end=False,
                        )
                    except stripe.StripeError as e:
                        logger.warning("widget_subscription: Stripe Subscription.modify metadata/cancel_at_period_end failed sub_id=%s err=%s", sub.stripe_subscription_id, e)
                    # Use latest_invoice from the modify response (the invoice Stripe just created for this change).
                    latest_invoice = getattr(stripe_sub, "latest_invoice", None) or stripe_sub.get("latest_invoice")
                    latest_inv_id = None
                    latest_inv_status = None
                    if latest_invoice:
                        if isinstance(latest_invoice, str):
                            latest_inv_id = latest_invoice
                            logger.info(
                                "widget_subscription: after modify latest_invoice is string id=%s (not expanded)",
                                latest_inv_id,
                            )
                        else:
                            latest_inv_id = getattr(latest_invoice, "id", None) or (latest_invoice.get("id") if isinstance(latest_invoice, dict) else None)
                            latest_inv_status = getattr(latest_invoice, "status", None) or (latest_invoice.get("status") if isinstance(latest_invoice, dict) else None)
                            logger.info(
                                "widget_subscription: after modify latest_invoice object inv_id=%s status=%s",
                                latest_inv_id,
                                latest_inv_status,
                            )
                    else:
                        logger.info("widget_subscription: after modify latest_invoice is None")
                    client_secret = _client_secret_from_stripe_invoice(latest_invoice) if latest_invoice and not isinstance(latest_invoice, str) else None
                    # If latest_invoice was only an id (expand not applied), retrieve the invoice and get PI.
                    if not client_secret and latest_inv_id and isinstance(latest_invoice, str):
                        try:
                            inv_obj = stripe.Invoice.retrieve(latest_inv_id, expand=["payment_intent", "payments"])
                            client_secret = _client_secret_from_stripe_invoice(inv_obj)
                            if client_secret:
                                logger.info("widget_subscription: got client_secret from Invoice.retrieve(inv_id=%s)", latest_inv_id)
                        except stripe.StripeError as e:
                            logger.warning("widget_subscription: Invoice.retrieve failed inv_id=%s err=%s", latest_inv_id, e)
                    if client_secret:
                        logger.info(
                            "widget_subscription: returning requires_payment client_secret from latest_invoice sub_id=%s",
                            sub.stripe_subscription_id,
                        )
                    # If no client_secret from latest_invoice, try listing open invoices for this subscription (fallback if latest_invoice was not updated).
                    if not client_secret and sub.stripe_subscription_id:
                        try:
                            open_invoices = stripe.Invoice.list(
                                subscription=sub.stripe_subscription_id,
                                status="open",
                                limit=1,
                            )
                            open_data = open_invoices.get("data") or []
                            logger.info(
                                "widget_subscription: open_invoices fallback sub_id=%s count=%s",
                                sub.stripe_subscription_id,
                                len(open_data),
                            )
                            if open_data:
                                client_secret = _client_secret_from_stripe_invoice(open_data[0])
                                if client_secret:
                                    logger.info("widget_subscription: got client_secret from open_invoices fallback")
                        except stripe.StripeError as e:
                            logger.warning("widget_subscription: Invoice.list open failed sub_id=%s err=%s", sub.stripe_subscription_id, e)
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
                    # No client_secret: either no payment needed (e.g. downgrade) or PI was terminal (already used).
                    inv_status = latest_inv_status
                    if inv_status is None and latest_invoice and not isinstance(latest_invoice, str):
                        inv_status = getattr(latest_invoice, "status", None) or (
                            latest_invoice.get("status") if isinstance(latest_invoice, dict) else None
                        )
                    logger.info(
                        "widget_subscription: no client_secret inv_status=%s latest_inv_id=%s",
                        inv_status,
                        latest_inv_id,
                    )
                    if inv_status == "open":
                        # Invoice still open but we had no usable PI -> link expired or already used.
                        logger.info("widget_subscription: returning 400 invoice open but no usable PI")
                        return Response(
                            {"error": "This payment link is no longer valid. Please try switching plan again."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # If there is any open invoice for this subscription, do not sync plan — payment is required.
                    try:
                        open_invoices_check = stripe.Invoice.list(
                            subscription=sub.stripe_subscription_id,
                            status="open",
                            limit=1,
                        )
                        if (open_invoices_check.get("data") or []):
                            logger.info(
                                "widget_subscription: open invoice exists, not syncing plan sub_id=%s",
                                sub.stripe_subscription_id,
                            )
                            return Response(
                                {"error": "Payment is required to complete this plan change. Please complete payment when prompted or try again."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                    except stripe.StripeError as e:
                        logger.warning("widget_subscription: Invoice.list open check failed sub_id=%s err=%s", sub.stripe_subscription_id, e)
                    # Need subscription with items to check if new price was applied (for sync vs 400).
                    try:
                        stripe_sub = stripe.Subscription.retrieve(
                            sub.stripe_subscription_id,
                            expand=["items.data.price"],
                        )
                    except stripe.StripeError:
                        stripe_sub = stripe_sub  # keep modify response
                    stripe_items = (stripe_sub.get("items") or {}).get("data") or []
                    stripe_price_id_now = None
                    if stripe_items and stripe_items[0].get("price"):
                        stripe_price_id_now = stripe_items[0]["price"].get("id") if isinstance(stripe_items[0]["price"], dict) else getattr(stripe_items[0]["price"], "id", None)
                    logger.info(
                        "widget_subscription: stripe_price_id_now=%s target price_id=%s",
                        stripe_price_id_now,
                        price_id,
                    )
                    if stripe_price_id_now != price_id:
                        # Subscription in Stripe still has old price (pending update unpaid) – don't save; require payment.
                        logger.info("widget_subscription: returning 400 payment required (price not applied)")
                        return Response(
                            {"error": "Payment is required to complete this plan change. Please try again and complete payment when prompted."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # No payment required (e.g. downgrade credit) or invoice already paid. Sync from Stripe.
                    logger.info("widget_subscription: syncing plan from Stripe (no payment required or already paid)")
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
                    # Invoice may already be paid (e.g. default payment method); check Stripe and return success if active
                    try:
                        stripe_sub_fresh = stripe.Subscription.retrieve(stripe_sub.id)
                        if (stripe_sub_fresh.get("status") or "").strip().lower() in ("active", "trialing"):
                            synced, _ = sync_widget_subscription_from_stripe(
                                stripe_sub.id, subscription_obj=stripe_sub_fresh
                            )
                            if synced:
                                return Response(
                                    {
                                        "subscription": _subscription_response_from_sub(synced),
                                        "stripe_updated": True,
                                    },
                                    status=status.HTTP_200_OK,
                                )
                    except stripe.StripeError:
                        pass
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
                # Invoice may already be paid (e.g. default payment method); check Stripe and return success if active
                try:
                    stripe_sub_fresh = stripe.Subscription.retrieve(stripe_sub.id)
                    if (stripe_sub_fresh.get("status") or "").strip().lower() in ("active", "trialing"):
                        synced, _ = sync_widget_subscription_from_stripe(
                            stripe_sub.id, subscription_obj=stripe_sub_fresh
                        )
                        if synced:
                            return Response(
                                {
                                    "subscription": _subscription_response_from_sub(synced),
                                    "stripe_updated": True,
                                },
                                status=status.HTTP_200_OK,
                            )
                except stripe.StripeError:
                    pass
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
    """POST: Set cancel_at_period_end=True in Stripe. DB is synced from Stripe (single source of truth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not sub.stripe_subscription_id:
            sub.cancel_at_period_end = True
            sub.save(update_fields=["cancel_at_period_end"])
            return Response(
                {"subscription": _subscription_response_from_sub(sub)},
                status=status.HTTP_200_OK,
            )
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        except stripe.StripeError as e:
            logger.warning("Stripe Subscription.modify cancel_at_period_end failed: %s", e)
            return Response(
                {"error": "Could not update cancellation. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
        sub = synced or sub
        return Response(
            {"subscription": _subscription_response_from_sub(sub)},
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionReactivateView(APIView):
    """POST: Set cancel_at_period_end=False in Stripe. DB is synced from Stripe (single source of truth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_current_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not sub.stripe_subscription_id:
            sub.cancel_at_period_end = False
            sub.save(update_fields=["cancel_at_period_end"])
            return Response(
                {"subscription": _subscription_response_from_sub(sub)},
                status=status.HTTP_200_OK,
            )
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
        except stripe.StripeError as e:
            logger.warning("Stripe Subscription.modify cancel_at_period_end=False failed: %s", e)
            return Response(
                {"error": "Could not reactivate. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        synced, _ = sync_widget_subscription_from_stripe(sub.stripe_subscription_id)
        sub = synced or sub
        return Response(
            {"subscription": _subscription_response_from_sub(sub)},
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionInvoicesView(APIView):
    """GET: List Stripe invoices for the business customer (widget + add-ons)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        customer_id = getattr(business, "stripe_customer_id", None)
        if not customer_id:
            return Response({"invoices": []}, status=status.HTTP_200_OK)

        invoices_by_id = {}
        for stripe_status in ("paid", "open", "draft"):
            try:
                stripe_invoices = stripe.Invoice.list(
                    customer=customer_id,
                    status=stripe_status,
                    limit=50,
                    expand=["data.charge", "data.lines.data"],
                )
                for inv in stripe_invoices.get("data", []):
                    inv_id = inv.get("id")
                    if inv_id:
                        invoices_by_id[inv_id] = inv
            except stripe.StripeError as e:
                logger.warning("Stripe Invoice.list (customer, status=%s) failed: %s", stripe_status, e)

        invoices = []
        for inv in invoices_by_id.values():
            amount = (inv.get("amount_paid") or 0) / 100.0
            # For open/draft invoices amount_paid can be 0; use amount_due for visibility.
            if amount <= 0:
                amount = (inv.get("amount_due") or 0) / 100.0
            # Ignore zero-value invoices (free/fully credited), they are noise in billing UI.
            if amount <= 0:
                continue
            currency = (inv.get("currency") or "usd").upper()
            created = inv.get("created")
            if created:
                from datetime import datetime
                if isinstance(created, (int, float)):
                    created = datetime.utcfromtimestamp(created).isoformat() + "Z"
            # Card used to pay (from charge.payment_method_details)
            payment_method = None
            charge = inv.get("charge")
            if isinstance(charge, dict):
                card = (charge.get("payment_method_details") or {}).get("card") or {}
                if card.get("last4") or card.get("brand"):
                    payment_method = {
                        "brand": (card.get("brand") or "card").capitalize(),
                        "last4": card.get("last4") or "****",
                    }
            # Line items breakdown
            lines_data = (inv.get("lines") or {}).get("data") if isinstance(inv.get("lines"), dict) else []
            if not lines_data and hasattr(inv.get("lines"), "data"):
                lines_data = inv["lines"].data or []
            lines = []
            for line in (lines_data or []):
                line_amount = (line.get("amount") or 0) / 100.0
                line_currency = (line.get("currency") or inv.get("currency") or "usd").upper()
                lines.append({
                    "description": line.get("description") or "Charge",
                    "amount": line_amount,
                    "currency": line_currency,
                })
            invoices.append({
                "id": inv.get("id"),
                "number": inv.get("number") or inv.get("id"),
                "created": created,
                "amount_paid": amount,
                "currency": currency,
                "status": inv.get("status"),
                "invoice_pdf": inv.get("invoice_pdf"),
                "payment_method": payment_method,
                "lines": lines,
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


class CreateUpdatePaymentMethodSetupIntentView(APIView):
    """POST: Create a SetupIntent for updating the business's saved payment method (no charge)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        if not business.stripe_customer_id:
            customer = stripe.Customer.create(
                email=business.studentContactEmail,
                name=business.businessName,
                metadata={"business_id": str(business.businessId)},
            )
            business.stripe_customer_id = customer.id
            business.save(update_fields=["stripe_customer_id"])
        try:
            si = stripe.SetupIntent.create(
                customer=business.stripe_customer_id,
                usage="off_session",
                payment_method_types=["card"],
            )
            return Response(
                {"client_secret": si.client_secret},
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            logger.warning("SetupIntent.create failed for business %s: %s", business.businessId, e)
            return Response(
                {"error": "Could not prepare payment form. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class SetDefaultPaymentMethodView(APIView):
    """POST: Set the business's default payment method (payment_method id from client after confirmSetup)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        if not business.stripe_customer_id:
            return Response(
                {"error": "No billing account found. Subscribe to a plan first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_method = (request.data.get("payment_method") or "").strip()
        if not payment_method:
            return Response(
                {"error": "payment_method is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            pm = stripe.PaymentMethod.retrieve(payment_method)
            pm_customer = getattr(pm, "customer", None) or (pm.get("customer") if isinstance(pm, dict) else None)
            if pm_customer != business.stripe_customer_id:
                return Response(
                    {"error": "Invalid payment method."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            stripe.Customer.modify(
                business.stripe_customer_id,
                invoice_settings={"default_payment_method": payment_method},
            )
            sub = _get_current_subscription(business)
            if sub and sub.stripe_subscription_id:
                try:
                    stripe.Subscription.modify(
                        sub.stripe_subscription_id,
                        default_payment_method=payment_method,
                    )
                except stripe.StripeError as e:
                    logger.warning(
                        "Subscription.modify default_payment_method failed sub_id=%s: %s",
                        sub.stripe_subscription_id,
                        e,
                    )
            return Response({"success": True}, status=status.HTTP_200_OK)
        except stripe.StripeError as e:
            logger.warning("SetDefaultPaymentMethod failed for business %s: %s", business.businessId, e)
            return Response(
                {"error": "Could not update payment method. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class DefaultPaymentMethodView(APIView):
    """GET: Return masked default payment method (brand, last4) for UI, or null if none."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        pm_id = _get_business_default_payment_method_id(business)
        if not pm_id:
            return Response({"payment_method": None}, status=status.HTTP_200_OK)
        try:
            pm = stripe.PaymentMethod.retrieve(pm_id)
            card = getattr(pm, "card", None) or (pm.get("card") if isinstance(pm, dict) else None)
            if not card:
                return Response({"payment_method": None}, status=status.HTTP_200_OK)
            brand = getattr(card, "brand", None) or (card.get("brand") if isinstance(card, dict) else None)
            last4 = getattr(card, "last4", None) or (card.get("last4") if isinstance(card, dict) else None)
            return Response(
                {
                    "payment_method": {
                        "brand": (brand or "card").lower() if brand else "card",
                        "last4": last4 or "",
                    }
                },
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            logger.warning("PaymentMethod.retrieve failed pm_id=%s: %s", pm_id, e)
            return Response({"payment_method": None}, status=status.HTTP_200_OK)


class BusinessAddonsView(APIView):
    """GET: Return status of addon subscriptions for the authenticated business."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        addon = _get_current_addon_subscription(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
        # Single source of truth: sync from Stripe when we have a linked addon subscription
        if addon and addon.stripe_subscription_id:
            synced, _ = sync_addon_subscription_from_stripe(
                addon.stripe_subscription_id,
                addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
            )
            if synced:
                addon = synced
        elif addon is None:
            latest = (
                BusinessAddonSubscription.objects.filter(
                    business=business,
                    addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                )
                .exclude(stripe_subscription_id__isnull=True)
                .exclude(stripe_subscription_id="")
                .order_by("-created_at")
                .first()
            )
            if latest and latest.stripe_subscription_id:
                synced, _ = sync_addon_subscription_from_stripe(
                    latest.stripe_subscription_id,
                    addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                )
                if synced and synced.status in ("active", "trialing"):
                    addon = synced
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

        # Single source of truth: sync from Stripe (webhook will also run)
        synced, _ = sync_addon_subscription_from_stripe(
            stripe_sub.id,
            subscription_obj=stripe_sub,
            addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
        )
        if not synced:
            return Response(
                {"error": "Subscription created but could not sync. Please refresh the page."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        synced.current_period_end.isoformat() if synced.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": synced.cancel_at_period_end,
                }
            },
            status=status.HTTP_200_OK,
        )


class CancelMarketplaceEmailAddonView(APIView):
    """POST: Set cancel_at_period_end=True in Stripe. DB synced from Stripe (single source of truth)."""

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
            return Response(
                {"error": "Could not update cancellation. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        synced, _ = sync_addon_subscription_from_stripe(
            sub.stripe_subscription_id,
            addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
        )
        sub = synced or sub
        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": sub.cancel_at_period_end,
                }
            },
            status=status.HTTP_200_OK,
        )


class ReactivateMarketplaceEmailAddonView(APIView):
    """POST: Set cancel_at_period_end=False in Stripe. DB synced from Stripe (single source of truth)."""

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
            return Response(
                {"error": "Could not reactivate. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        synced, _ = sync_addon_subscription_from_stripe(
            sub.stripe_subscription_id,
            addon_type=ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
        )
        sub = synced or sub
        return Response(
            {
                "marketplace_email_branding": {
                    "active": True,
                    "currentPeriodEnd": (
                        sub.current_period_end.isoformat() if sub.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": sub.cancel_at_period_end,
                }
            },
            status=status.HTTP_200_OK,
        )
