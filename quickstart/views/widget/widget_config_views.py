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
    MembershipProduct,
    StripeCheckoutAttempt,
    WidgetSubscription,
    AuditLog,
)
from quickstart.models import ADDON_TYPE_EMAIL_MARKETING, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
from quickstart.services.email_marketing_config import list_public_tiers, price_id_to_tier
from quickstart.services.email_marketing_usage import usage_snapshot
from quickstart.serializers.widget.widget_config_serializer import (
    BusinessWidgetConfigSerializer,
    WidgetSubscriptionSerializer,
)
from quickstart.views.widget.widget_views import (
    _business_has_active_widget_subscription,
    build_widget_diagnostics_data,
)
from quickstart.services.subscription_sync import (
    sync_widget_subscription_from_stripe,
    sync_addon_subscription_from_stripe,
    mark_widget_subscription_canceled,
    mark_addon_subscription_canceled,
    _widget_price_to_plan_id,
)
from quickstart.utils.widget_booking_source import (
    business_has_growth_or_advanced_widget_plan,
)
from quickstart.services.widget_subscription_service import (
    get_widget_subscription,
    create_subscription,
    upgrade_subscription,
    downgrade_subscription,
    same_price_open_invoice,
    cancel_at_period_end as service_cancel_at_period_end,
    reactivate as service_reactivate,
    _get_price_id,
    _is_downgrade,
)
from quickstart.utils.email_branding_html import normalize_and_validate_branding_payload
from quickstart.utils.stripe_migration import stripe_migration_gone_response

stripe.api_key = settings.STRIPE_SECRET_KEY

# Plan order for upgrade/downgrade: lower index = lower tier
PLAN_ORDER = ["basic", "growth", "advanced"]


def _audit_widget_subscription_action(request, business, details: str):
    """Best-effort audit entry for SaaS widget subscription changes."""
    try:
        user = getattr(request, "user", None)
        AuditLog.objects.create(
            user=user if user and user.is_authenticated else None,
            user_email=getattr(user, "email", None) or "unknown",
            action="system_setting_change",
            details=(details or "")[:2000],
            target_model="WidgetSubscription",
            target_id=str(getattr(business, "businessId", "") or ""),
            metadata={"business_id": getattr(business, "businessId", None)},
        )
    except Exception as exc:
        logger.warning("Widget subscription audit log failed: %s", exc)


def _is_downgrade(from_plan_id, to_plan_id):
    if from_plan_id not in VALID_PLAN_IDS or to_plan_id not in VALID_PLAN_IDS:
        return False
    return PLAN_ORDER.index(to_plan_id) < PLAN_ORDER.index(from_plan_id)

VALID_PLAN_IDS = {"basic", "growth", "advanced"}


def _obj_get(obj, key, default=None):
    """Read a field from dict-like or StripeObject without calling .get on StripeObject."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


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
    invoice = getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
    if invoice is not None and not isinstance(invoice, str):
        secret = _subscription_client_secret_from_invoice(invoice)
        if secret:
            return secret
    if stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(stripe_sub.id, expand=["latest_invoice.payments"])
            inv = getattr(expanded, "latest_invoice", None) or _obj_get(expanded, "latest_invoice")
            if inv is not None and not isinstance(inv, str):
                secret = _subscription_client_secret_from_invoice(inv)
                if secret:
                    return secret
        except stripe.StripeError:
            pass
    latest_invoice = getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
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
        sub = get_widget_subscription(business)
        if sub and (sub.status or "").strip().lower() in ("active", "trialing"):
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

        # Active membership products for widget customizer subscription copy-paste snippets
        membership_products = MembershipProduct.objects.filter(
            business=business, is_active=True
        ).order_by("price")
        response_data["membership_products"] = [
            {
                "id": str(p.id),
                "name": p.name,
                "badge_text": getattr(p, "badge_text", "") or "",
                "price": str(p.price),
                "billing_interval": p.billing_interval,
                "is_active": True,
                "widget_button_config": getattr(p, "widget_button_config", None) or {},
                "widget_features": getattr(p, "widget_features", None) or {},
                "widget_cta_label": getattr(p, "widget_cta_label", "") or "",
            }
            for p in membership_products
        ]

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
            sub = get_widget_subscription(business)
            plan_id = (
                (sub.plan_id or "").strip().lower()
                if sub and (sub.status or "").strip().lower() in ("active", "trialing")
                else None
            )
            if plan_id not in ("growth", "advanced"):
                return Response(
                    {
                        "detail": "Personalized booking emails (widget email branding) require a Growth or Advanced widget plan.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if isinstance(widget_email_branding, dict):
                ok, err_list, cleaned = normalize_and_validate_branding_payload(
                    widget_email_branding
                )
                if not ok:
                    return Response(
                        {"detail": "Invalid email branding.", "errors": err_list},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                business.widget_email_branding = cleaned
                business.save(update_fields=["widget_email_branding"])

        # marketplace_email_branding_enabled is set only by addon subscription webhook; we do not accept it here.
        data.pop("marketplace_email_branding_enabled", None)
        marketplace_branding = data.pop("marketplace_email_branding", None)
        if marketplace_branding is not None and isinstance(marketplace_branding, dict):
            ok, err_list, cleaned = normalize_and_validate_branding_payload(
                marketplace_branding
            )
            if not ok:
                return Response(
                    {"detail": "Invalid marketplace email branding.", "errors": err_list},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            business.marketplace_email_branding = cleaned
            business.save(update_fields=["marketplace_email_branding"])

        # Pin widget to a specific class (specificClassId) is Growth/Advanced only.
        sub = get_widget_subscription(business)
        plan_id = (
            (sub.plan_id or "").strip().lower()
            if sub and (sub.status or "").strip().lower() in ("active", "trialing")
            else None
        )
        if plan_id not in ("growth", "advanced"):
            if data.get("specificClassId"):
                return Response(
                    {
                        "detail": "Pin widget to a specific class requires a Growth or Advanced widget plan.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Allow null/empty specificClassId through so the serializer can clear a stored global pin.

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

        interval = (request.data.get("billing_interval") or request.data.get("interval") or "month").strip().lower()
        if interval not in ("month", "year"):
            interval = "month"

        monthly_map = {
            "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC", None),
            "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH", None),
            "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED", None),
        }
        annual_map = {
            "basic": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_BASIC_ANNUAL", None),
            "growth": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_GROWTH_ANNUAL", None),
            "advanced": getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ADVANCED_ANNUAL", None),
        }
        if interval == "year":
            price_id = annual_map.get(plan_id) or monthly_map.get(plan_id)
        else:
            price_id = monthly_map.get(plan_id) or getattr(settings, "WIDGET_SUBSCRIPTION_PRICE_ID", None)
        if not price_id:
            return Response(
                {"error": "Widget subscription is not configured. Please set Stripe Price IDs in settings."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        business = self.get_business(request.user)
        base_fe = (getattr(settings, "FRONTEND_BASE_URL", None) or "").rstrip("/")
        if not base_fe:
            base_fe = request.build_absolute_uri("/").rstrip("/")
        default_success = f"{base_fe}/business/dashboard?tab=settings&checkout=success"
        default_cancel = f"{base_fe}/business/dashboard?tab=settings&checkout=cancel"
        success_url = (request.data.get("success_url") or default_success).strip()
        cancel_url = (request.data.get("cancel_url") or default_cancel).strip()
        try:
            session_params = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url
                + ("&" if "?" in success_url else "?")
                + "session_id={CHECKOUT_SESSION_ID}&subscribed=1",
                "cancel_url": cancel_url + ("&" if "?" in cancel_url else "?") + f"plan={plan_id}",
                "client_reference_id": str(business.businessId),
                "metadata": {
                    "business_id": str(business.businessId),
                    "plan_id": plan_id,
                    "product_type": "widget",
                    "billing_interval": interval,
                },
                "subscription_data": {
                    "metadata": {
                        "business_id": str(business.businessId),
                        "plan_id": plan_id,
                    },
                },
            }
            if getattr(settings, "STRIPE_CHECKOUT_AUTOMATIC_TAX", False):
                session_params["automatic_tax"] = {"enabled": True}
                session_params["customer_update"] = {"address": "auto"}
                session_params["billing_address_collection"] = "required"
            if business.stripe_customer_id:
                session_params["customer"] = business.stripe_customer_id
            else:
                session_params["customer_email"] = business.studentContactEmail
            session = stripe.checkout.Session.create(**session_params)
            StripeCheckoutAttempt.objects.create(
                business=business,
                checkout_session_id=session.id,
                product_type="widget",
                plan_or_tier_key=plan_id,
                stripe_price_id=price_id or "",
            )
            return Response(
                {
                    "url": session.url,
                    "checkout_url": session.url,
                    "session_id": session.id,
                },
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class CreateBillingPortalSessionView(APIView):
    """POST: Return a Stripe Customer Portal URL (manage/cancel subscriptions, payment methods)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = BusinessInfo.objects.filter(
            Q(owner=request.user) | Q(staff_members__user=request.user, staff_members__status="accepted")
        ).first()
        if not business:
            from rest_framework.exceptions import NotFound

            raise NotFound("You are not a member of any business.")
        if not business.stripe_customer_id:
            return Response(
                {
                    "error": "no_stripe_customer",
                    "detail": "Complete a subscription checkout once to manage billing.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        base_fe = (getattr(settings, "FRONTEND_BASE_URL", None) or "").rstrip("/")
        if not base_fe:
            base_fe = request.build_absolute_uri("/").rstrip("/")
        return_url = (request.data.get("return_url") or f"{base_fe}/business/dashboard?tab=settings").strip()
        params = {
            "customer": business.stripe_customer_id,
            "return_url": return_url,
        }
        flow = (request.data.get("flow") or "").strip()
        subscription_id = (request.data.get("subscription_id") or "").strip()
        if flow == "subscription_update":
            if not subscription_id:
                return Response(
                    {"error": "subscription_id is required for subscription_update."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                stripe_sub = stripe.Subscription.retrieve(subscription_id)
                sub_customer = _obj_get(stripe_sub, "customer")
                if sub_customer != business.stripe_customer_id:
                    return Response(
                        {"error": "That subscription does not belong to this account."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except stripe.InvalidRequestError:
                return Response(
                    {"error": "Subscription not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            except stripe.StripeError as e:
                logger.warning("Billing Portal subscription verify failed: %s", e)
                return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
            params["flow_data"] = {
                "type": "subscription_update",
                "subscription_update": {"subscription": subscription_id},
            }
        cfg = getattr(settings, "STRIPE_BILLING_PORTAL_CONFIGURATION_ID", "") or ""
        if cfg:
            params["configuration"] = cfg
        try:
            session = stripe.billing_portal.Session.create(**params)
            return Response(
                {"url": session.url, "portal_url": session.url},
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            logger.warning("Billing Portal session failed business=%s: %s", business.businessId, e)
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


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
        gone = stripe_migration_gone_response()
        if gone is not None:
            return gone
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
        active_sub = get_widget_subscription(business)
        if (
            active_sub
            and (active_sub.status or "").strip().lower() in ("active", "trialing")
            and (active_sub.current_period_end is None or active_sub.current_period_end > now)
        ):
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

            # Cancel any leftover incomplete subscription (one row per business) to avoid stale intents
            incomplete_sub = get_widget_subscription(business)
            if (
                incomplete_sub
                and (incomplete_sub.status or "").strip().lower() == "incomplete"
                and incomplete_sub.stripe_subscription_id
            ):
                try:
                    stripe.Subscription.cancel(incomplete_sub.stripe_subscription_id)
                except stripe.StripeError:
                    pass
                # Row is kept; create flow will update it with the new Stripe sub

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
            invoice = getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
            if invoice is not None and not isinstance(invoice, str):
                client_secret = _client_secret_from_invoice(invoice)

            # 2) Re-retrieve subscription with expand (create often omits nested expand)
            if not client_secret and stripe_sub.id:
                try:
                    stripe_sub_expanded = stripe.Subscription.retrieve(
                        stripe_sub.id,
                        expand=["latest_invoice.payments"],
                    )
                    inv = getattr(stripe_sub_expanded, "latest_invoice", None) or _obj_get(stripe_sub_expanded, "latest_invoice")
                    if inv is not None and not isinstance(inv, str):
                        client_secret = _client_secret_from_invoice(inv)
                except stripe.StripeError as e:
                    logger.warning("Subscription.retrieve expand failed: %s", e)

            # 3) Fallback: get invoice id and retrieve invoice with payments expanded
            if not client_secret:
                latest_invoice = getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
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
                    if (_obj_get(stripe_sub_fresh, "status", "") or "").strip().lower() in ("active", "trialing"):
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

            # Attach new Stripe subscription to the single row for this business (one per business)
            stripe_price_id = None
            items = _obj_get(stripe_sub, "items", {}) or {}
            items_data = _obj_get(items, "data", []) or []
            if items_data:
                first_item = items_data[0]
                price_obj = _obj_get(first_item, "price", {}) or {}
                stripe_price_id = price_obj if isinstance(price_obj, str) else _obj_get(price_obj, "id")

            widget_sub, _ = WidgetSubscription.objects.get_or_create(
                business=business,
                defaults={
                    "stripe_subscription_id": stripe_sub.id,
                    "stripe_customer_id": business.stripe_customer_id,
                    "stripe_price_id": stripe_price_id,
                    "status": stripe_sub.status,
                    "plan_id": plan_id,
                },
            )
            if not _:
                widget_sub.stripe_subscription_id = stripe_sub.id
                widget_sub.stripe_customer_id = business.stripe_customer_id
                widget_sub.stripe_price_id = stripe_price_id
                widget_sub.status = stripe_sub.status
                widget_sub.plan_id = plan_id
                widget_sub.save(
                    update_fields=[
                        "stripe_subscription_id",
                        "stripe_customer_id",
                        "stripe_price_id",
                        "status",
                        "plan_id",
                    ]
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

    # Cancel any leftover incomplete subscription (one row per business)
    incomplete_sub = get_widget_subscription(business)
    if (
        incomplete_sub
        and (incomplete_sub.status or "").strip().lower() == "incomplete"
        and incomplete_sub.stripe_subscription_id
    ):
        try:
            stripe.Subscription.cancel(incomplete_sub.stripe_subscription_id)
        except stripe.StripeError:
            pass

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
        getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
    )
    if not client_secret and stripe_sub.id:
        try:
            expanded = stripe.Subscription.retrieve(
                stripe_sub.id,
                expand=["latest_invoice.payments"],
            )
            inv = getattr(expanded, "latest_invoice", None) or _obj_get(expanded, "latest_invoice")
            client_secret = _client_secret_from_stripe_invoice(inv)
        except stripe.StripeError:
            pass
    if not client_secret:
        latest_inv = getattr(stripe_sub, "latest_invoice", None) or _obj_get(stripe_sub, "latest_invoice")
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
        "paymentGraceUntil": (
            sub.payment_grace_until.isoformat()
            if getattr(sub, "payment_grace_until", None)
            else None
        ),
        "cancelAtPeriodEnd": sub.cancel_at_period_end,
        "stripeSubscriptionId": sub.stripe_subscription_id or None,
        "compReason": (getattr(sub, "comp_reason", None) or None),
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


class BusinessWidgetDiagnosticsView(APIView):
    """Widget install diagnostics for the authenticated business (session auth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        raw_ref = (
            request.query_params.get("referrer")
            or request.query_params.get("origin")
            or ""
        )
        return Response(build_widget_diagnostics_data(business, raw_ref))


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
        sub = get_widget_subscription(business)
        subscription_required = getattr(settings, "WIDGET_SUBSCRIPTION_REQUIRED", False)
        has_widget_access = _business_has_active_widget_subscription(business)
        has_widget_analytics = business_has_growth_or_advanced_widget_plan(business)
        # Same resolver as BusinessAddonsView / email marketing APIs (Stripe sync, stale period_end).
        em_addon = resolve_email_marketing_addon_subscription(business)
        business.refresh_from_db(fields=["email_marketing_enabled"])
        has_email_marketing_access = bool(
            em_addon and getattr(business, "email_marketing_enabled", False)
        )
        if not sub:
            return Response(
                {
                    "subscription": None,
                    "widget_subscription_required": subscription_required,
                    "has_stripe_subscription": False,
                    "has_widget_access": has_widget_access,
                    "has_widget_analytics": has_widget_analytics,
                    "has_membership_access": False,
                    "has_email_marketing_access": has_email_marketing_access,
                },
                status=status.HTTP_200_OK,
            )
        plan_id = (sub.plan_id or "").strip().lower()
        sub_status = (sub.status or "").strip().lower()
        # Memberships require an entitled SaaS tier *and* a healthy billing state (not past_due grace).
        membership_billing_ok = sub_status in ("active", "trialing")
        has_membership_access = (
            has_widget_access
            and plan_id in ("growth", "advanced")
            and membership_billing_ok
        )
        payload = {
            "subscription": _subscription_response_from_sub(sub),
            "widget_subscription_required": subscription_required,
            "has_stripe_subscription": bool(sub.stripe_subscription_id),
            "has_widget_access": has_widget_access,
            "has_widget_analytics": has_widget_analytics,
            "has_membership_access": has_membership_access,
            "has_email_marketing_access": has_email_marketing_access,
        }
        # Optional: scheduled downgrade from Stripe subscription schedule
        if sub.stripe_subscription_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(
                    sub.stripe_subscription_id,
                    expand=["schedule"],
                )
                sched = _obj_get(stripe_sub, "schedule")
                schedule_id = None
                if isinstance(sched, str):
                    schedule_id = sched
                elif sched is not None:
                    schedule_id = sched.get("id") if isinstance(sched, dict) else getattr(sched, "id", None)
                if schedule_id:
                    schedule = stripe.SubscriptionSchedule.retrieve(schedule_id, expand=["phases"])
                    phases = getattr(schedule, "phases", None) or _obj_get(schedule, "phases", []) or []
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
        price_id = _get_price_id(plan_id)
        if not price_id:
            return Response(
                {"error": "Widget subscription pricing is not configured. Please contact support."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        sub = get_widget_subscription(business)

        with transaction.atomic():
            if sub:
                sub = WidgetSubscription.objects.select_for_update().get(pk=sub.pk)
            else:
                BusinessInfo.objects.select_for_update().get(pk=business.pk)

            # No row, or row has no Stripe sub or is not active/trialing: create (or replace) subscription
            if not sub or not sub.stripe_subscription_id or (sub.status or "").strip().lower() not in ("active", "trialing"):
                try:
                    stripe_sub, client_secret, sub = create_subscription(business, plan_id)
                except ValueError as e:
                    return Response(
                        {"error": str(e)},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                except stripe.StripeError as e:
                    logger.warning("Stripe subscription create failed: %s", e)
                    return Response(
                        {"error": str(e)},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
                if not client_secret:
                    try:
                        stripe_sub_fresh = stripe.Subscription.retrieve(stripe_sub.id)
                        if (_obj_get(stripe_sub_fresh, "status", "") or "").strip().lower() in ("active", "trialing"):
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

            # Existing active/trialing subscription: same price, downgrade, or upgrade
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
            items_data = _obj_get(stripe_sub, "items", {}) or {}
            item_list = _obj_get(items_data, "data", []) or []
            if not item_list:
                return Response(
                    {"error": "Invalid subscription state. Please contact support."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            current_price = _obj_get(item_list[0], "price", {}) if item_list else {}
            current_price_id = current_price if isinstance(current_price, str) else _obj_get(current_price, "id")

            if current_price_id == price_id:
                req_pay, client_secret, sub_resp = same_price_open_invoice(sub, plan_id, price_id)
                if req_pay is True and client_secret:
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
                if req_pay is None:
                    return Response(
                        {"error": "Payment is required to complete this plan change. Please complete payment when prompted or try again."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                return Response(
                    {
                        "subscription": _subscription_response_from_sub(sub_resp),
                        "stripe_updated": True,
                    },
                    status=status.HTTP_200_OK,
                )

            current_plan_id = _widget_price_to_plan_id(current_price_id) or (sub.plan_id or "").strip().lower() or "growth"
            if _is_downgrade(current_plan_id, plan_id):
                synced_sub, err = downgrade_subscription(sub, plan_id, business)
                if err:
                    return Response(
                        {"error": err},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                return Response(
                    {
                        "subscription": _subscription_response_from_sub(synced_sub),
                        "stripe_updated": True,
                        "downgrade_scheduled_at_period_end": True,
                        "scheduled_plan_id": plan_id,
                    },
                    status=status.HTTP_200_OK,
                )

            requires_payment, client_secret, updated_sub, err = upgrade_subscription(sub, plan_id, business)
            if err:
                return Response(
                    {"error": err},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if requires_payment and client_secret:
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
                {
                    "subscription": _subscription_response_from_sub(updated_sub),
                    "stripe_updated": True,
                },
                status=status.HTTP_200_OK,
            )


class WidgetSubscriptionCancelView(APIView):
    """POST: Set cancel_at_period_end=True in Stripe. DB is synced from Stripe (single source of truth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = get_widget_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not sub.stripe_subscription_id:
            sub.cancel_at_period_end = True
            sub.save(update_fields=["cancel_at_period_end"])
            _audit_widget_subscription_action(
                request, business, "Widget subscription: cancel_at_period_end set (no Stripe id)"
            )
            return Response(
                {"subscription": _subscription_response_from_sub(sub)},
                status=status.HTTP_200_OK,
            )
        synced, err = service_cancel_at_period_end(sub)
        if err:
            return Response(
                {"error": err or "Could not update cancellation. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        sub = synced or sub
        _audit_widget_subscription_action(
            request, business, "Widget subscription: cancel at period end requested in Stripe"
        )
        return Response(
            {"subscription": _subscription_response_from_sub(sub)},
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionReactivateView(APIView):
    """POST: Set cancel_at_period_end=False in Stripe. DB is synced from Stripe (single source of truth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = get_widget_subscription(business)
        if not sub:
            return Response(
                {"error": "No active subscription found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not sub.stripe_subscription_id:
            sub.cancel_at_period_end = False
            sub.save(update_fields=["cancel_at_period_end"])
            _audit_widget_subscription_action(
                request, business, "Widget subscription: reactivate (cleared cancel_at_period_end, no Stripe)"
            )
            return Response(
                {"subscription": _subscription_response_from_sub(sub)},
                status=status.HTTP_200_OK,
            )
        synced, err = service_reactivate(sub)
        if err:
            return Response(
                {"error": err or "Could not reactivate. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        sub = synced or sub
        _audit_widget_subscription_action(
            request, business, "Widget subscription: reactivate in Stripe (cleared cancel_at_period_end)"
        )
        return Response(
            {"subscription": _subscription_response_from_sub(sub)},
            status=status.HTTP_200_OK,
        )


class WidgetSubscriptionInvoicesView(APIView):
    """GET: List invoices from Stripe for this customer (source of truth; no local invoice DB)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        customer_id = getattr(business, "stripe_customer_id", None)
        if not customer_id:
            return Response({"invoices": []}, status=status.HTTP_200_OK)

        invoices_by_id = {}
        try:
            stripe_invoices = stripe.Invoice.list(
                customer=customer_id,
                limit=100,
                expand=["data.charge", "data.lines.data"],
            )
            for inv in _obj_get(stripe_invoices, "data", []) or []:
                inv_id = _obj_get(inv, "id")
                if inv_id:
                    invoices_by_id[inv_id] = inv
        except stripe.StripeError as e:
            logger.warning("Stripe Invoice.list (customer) failed: %s", e)

        invoices = []
        for inv in invoices_by_id.values():
            inv_status = (_obj_get(inv, "status") or "").lower()
            if inv_status == "draft":
                continue
            amount_paid = (_obj_get(inv, "amount_paid") or 0) / 100.0
            amount_due = (_obj_get(inv, "amount_due") or 0) / 100.0
            if inv_status == "paid" and amount_paid <= 0:
                continue
            if inv_status == "open" and amount_due <= 0:
                continue

            currency = (_obj_get(inv, "currency") or "usd").upper()
            created = _obj_get(inv, "created")
            if created:
                from datetime import datetime
                if isinstance(created, (int, float)):
                    created = datetime.utcfromtimestamp(created).isoformat() + "Z"
            payment_method = None
            charge = _obj_get(inv, "charge")
            if charge:
                payment_details = _obj_get(charge, "payment_method_details", {}) or {}
                card = _obj_get(payment_details, "card", {}) or {}
                if _obj_get(card, "last4") or _obj_get(card, "brand"):
                    payment_method = {
                        "brand": (_obj_get(card, "brand") or "card").capitalize(),
                        "last4": _obj_get(card, "last4") or "****",
                    }
            inv_lines = _obj_get(inv, "lines")
            lines_data = _obj_get(inv_lines, "data", []) or []
            lines = []
            for line in (lines_data or []):
                line_amount = (_obj_get(line, "amount") or 0) / 100.0
                line_currency = (_obj_get(line, "currency") or _obj_get(inv, "currency") or "usd").upper()
                lines.append({
                    "description": _obj_get(line, "description") or "Charge",
                    "amount": line_amount,
                    "currency": line_currency,
                })
            invoices.append({
                "id": _obj_get(inv, "id"),
                "number": _obj_get(inv, "number") or _obj_get(inv, "id"),
                "created": created,
                "amount_paid": amount_paid,
                "amount_due": amount_due,
                "currency": currency,
                "status": _obj_get(inv, "status"),
                "invoice_pdf": _obj_get(inv, "invoice_pdf"),
                "hosted_invoice_url": _obj_get(inv, "hosted_invoice_url"),
                "payment_method": payment_method,
                "lines": lines,
            })
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


def resolve_email_marketing_addon_subscription(business):
    """
    Effective email-marketing addon row for this business.

    Matches BusinessAddonsView: prefer in-period active/trialing subscription; if none
    (e.g. stale current_period_end in DB), fall back to latest row with a Stripe id and
    sync from Stripe. Keeps marketing API routes aligned with what the dashboard shows
    as subscribed.
    """
    em_addon = _get_current_addon_subscription(business, ADDON_TYPE_EMAIL_MARKETING)
    if em_addon and em_addon.stripe_subscription_id:
        synced_em, _ = sync_addon_subscription_from_stripe(
            em_addon.stripe_subscription_id,
            addon_type=ADDON_TYPE_EMAIL_MARKETING,
        )
        if synced_em:
            em_addon = synced_em
    elif em_addon is None:
        latest_em = (
            BusinessAddonSubscription.objects.filter(
                business=business,
                addon_type=ADDON_TYPE_EMAIL_MARKETING,
            )
            .exclude(stripe_subscription_id__isnull=True)
            .exclude(stripe_subscription_id="")
            .order_by("-created_at")
            .first()
        )
        if latest_em and latest_em.stripe_subscription_id:
            synced_em, _ = sync_addon_subscription_from_stripe(
                latest_em.stripe_subscription_id,
                addon_type=ADDON_TYPE_EMAIL_MARKETING,
            )
            if synced_em and synced_em.status in ("active", "trialing"):
                em_addon = synced_em
    return em_addon


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
        widget_sub = get_widget_subscription(business)
        if (
            widget_sub
            and widget_sub.stripe_subscription_id
            and (widget_sub.status or "").strip().lower() in ("active", "trialing")
            and (widget_sub.current_period_end is None or widget_sub.current_period_end > now)
        ):
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
        gone = stripe_migration_gone_response()
        if gone is not None:
            return gone
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
        gone = stripe_migration_gone_response()
        if gone is not None:
            return gone
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
            sub = get_widget_subscription(business)
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


def _serialize_stripe_card_payment_method(pm):
    """Build a dict for API from a Stripe PaymentMethod with type card."""
    if not pm:
        return None
    pm_id = getattr(pm, "id", None) or (pm.get("id") if isinstance(pm, dict) else None)
    card = getattr(pm, "card", None) or (pm.get("card") if isinstance(pm, dict) else None)
    if not card:
        return None
    brand = getattr(card, "brand", None) or (card.get("brand") if isinstance(card, dict) else None)
    last4 = getattr(card, "last4", None) or (card.get("last4") if isinstance(card, dict) else None)
    exp_month = getattr(card, "exp_month", None) or (card.get("exp_month") if isinstance(card, dict) else None)
    exp_year = getattr(card, "exp_year", None) or (card.get("exp_year") if isinstance(card, dict) else None)
    return {
        "id": pm_id,
        "brand": (brand or "card").lower() if brand else "card",
        "last4": last4 or "",
        "exp_month": int(exp_month) if exp_month is not None else None,
        "exp_year": int(exp_year) if exp_year is not None else None,
    }


class DefaultPaymentMethodView(APIView):
    """
    GET: Return saved card payment methods for the business Stripe customer, plus default id.
    Includes backward-compatible `payment_method` (the default card, same shape as before plus id/exp).
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        default_id = _get_business_default_payment_method_id(business)
        methods_out = []
        seen_ids = set()

        if business.stripe_customer_id:
            try:
                pms = stripe.PaymentMethod.list(
                    customer=business.stripe_customer_id,
                    type="card",
                    limit=100,
                )
                for pm in pms.data:
                    row = _serialize_stripe_card_payment_method(pm)
                    if row and row.get("id"):
                        methods_out.append(row)
                        seen_ids.add(row["id"])
            except stripe.StripeError as e:
                logger.warning(
                    "PaymentMethod.list failed customer=%s: %s",
                    business.stripe_customer_id,
                    e,
                )

        if default_id and default_id not in seen_ids:
            try:
                pm = stripe.PaymentMethod.retrieve(default_id)
                row = _serialize_stripe_card_payment_method(pm)
                if row and row.get("id"):
                    methods_out.insert(0, row)
                    seen_ids.add(row["id"])
            except stripe.StripeError as e:
                logger.warning("PaymentMethod.retrieve failed pm_id=%s: %s", default_id, e)

        methods_out.sort(
            key=lambda m: (0 if m.get("id") == default_id else 1, m.get("last4") or ""),
        )

        default_row = next((m for m in methods_out if m.get("id") == default_id), None)

        return Response(
            {
                "default_payment_method_id": default_id,
                "payment_methods": methods_out,
                "payment_method": default_row,
            },
            status=status.HTTP_200_OK,
        )


class DetachBusinessPaymentMethodView(APIView):
    """
    POST: Detach a saved card from the business Stripe customer.
    At least one card must remain attached.
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        gone = stripe_migration_gone_response()
        if gone is not None:
            return gone
        business = _get_business_for_subscription(request.user)
        if not business.stripe_customer_id:
            return Response(
                {"error": "No billing account found."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_method = (request.data.get("payment_method") or "").strip()
        if not payment_method:
            return Response(
                {"error": "payment_method is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            pms = stripe.PaymentMethod.list(
                customer=business.stripe_customer_id,
                type="card",
                limit=100,
            )
            attached_ids = [pm.id for pm in pms.data if getattr(pm, "id", None)]
            if payment_method not in attached_ids:
                return Response(
                    {"error": "Invalid payment method."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(attached_ids) <= 1:
                return Response(
                    {"error": "You must keep at least one payment method on file."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            default_id = _get_business_default_payment_method_id(business)
            if default_id == payment_method:
                other = next((pid for pid in attached_ids if pid != payment_method), None)
                if not other:
                    return Response(
                        {"error": "You must keep at least one payment method on file."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                stripe.Customer.modify(
                    business.stripe_customer_id,
                    invoice_settings={"default_payment_method": other},
                )
                sub = get_widget_subscription(business)
                if sub and sub.stripe_subscription_id:
                    try:
                        stripe.Subscription.modify(
                            sub.stripe_subscription_id,
                            default_payment_method=other,
                        )
                    except stripe.StripeError as e:
                        logger.warning(
                            "Subscription.modify default_payment_method after detach prep failed sub_id=%s: %s",
                            sub.stripe_subscription_id,
                            e,
                        )

            stripe.PaymentMethod.detach(payment_method)
            return Response({"success": True}, status=status.HTTP_200_OK)
        except stripe.StripeError as e:
            logger.warning(
                "DetachBusinessPaymentMethod failed for business %s: %s",
                business.businessId,
                e,
            )
            return Response(
                {"error": "Could not remove payment method. Please try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )


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
        # --- Email marketing addon (ladder tiers) ---
        em_addon = resolve_email_marketing_addon_subscription(business)
        em_usage = usage_snapshot(business, em_addon) if em_addon else None
        em_can_instant = em_addon is None and _business_can_instant_subscribe(business)
        em_current_price_id = (em_addon.stripe_price_id or "") if em_addon else ""
        em_tier_info = price_id_to_tier(em_current_price_id) if em_current_price_id else None

        data = {
            "marketplace_email_branding": {
                "active": addon is not None,
                "currentPeriodEnd": (
                    addon.current_period_end.isoformat() if addon and addon.current_period_end else None
                ),
                "cancelAtPeriodEnd": addon.cancel_at_period_end if addon else False,
                "canInstantSubscribe": can_instant,
            },
            "email_marketing": {
                "active": em_addon is not None,
                "currentPeriodEnd": (
                    em_addon.current_period_end.isoformat() if em_addon and em_addon.current_period_end else None
                ),
                "cancelAtPeriodEnd": em_addon.cancel_at_period_end if em_addon else False,
                "canInstantSubscribe": em_can_instant,
                "usage": em_usage,
                "current_price_id": em_current_price_id or None,
                "current_tier_key": em_tier_info["tier_key"] if em_tier_info else None,
                "tiers": list_public_tiers(),
                "transactional_emails_excluded_from_quota": True,
            },
        }
        return Response(data, status=status.HTTP_200_OK)


class CreateMarketplaceEmailAddonCheckoutView(APIView):
    """POST: Create Stripe Checkout Session for marketplace email branding addon ($7/mo)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        interval = (request.data.get("billing_interval") or request.data.get("interval") or "month").strip().lower()
        if interval not in ("month", "year"):
            interval = "month"
        monthly_pid = getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID", None)
        annual_pid = getattr(settings, "MARKETPLACE_EMAIL_ADDON_PRICE_ID_ANNUAL", None)
        price_id = annual_pid if interval == "year" else monthly_pid
        if not price_id:
            price_id = monthly_pid or annual_pid
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
        base_fe = (getattr(settings, "FRONTEND_BASE_URL", None) or "").rstrip("/") or request.build_absolute_uri("/").rstrip("/")
        default_success = f"{base_fe}/business/dashboard?tab=settings&checkout=success&addon=marketplace_email"
        default_cancel = f"{base_fe}/business/dashboard?tab=settings&checkout=cancel"
        success_url = (request.data.get("success_url") or default_success).strip()
        cancel_url = (request.data.get("cancel_url") or default_cancel).strip()
        try:
            session_params = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url
                + ("&" if "?" in success_url else "?")
                + "session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": cancel_url,
                "client_reference_id": str(business.businessId),
                "metadata": {
                    "business_id": str(business.businessId),
                    "product_type": "marketplace_email_branding",
                    "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                    "billing_interval": interval,
                },
                "subscription_data": {
                    "metadata": {
                        "business_id": str(business.businessId),
                        "addon_type": ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                    },
                },
            }
            if getattr(settings, "STRIPE_CHECKOUT_AUTOMATIC_TAX", False):
                session_params["automatic_tax"] = {"enabled": True}
                session_params["customer_update"] = {"address": "auto"}
                session_params["billing_address_collection"] = "required"
            if business.stripe_customer_id:
                session_params["customer"] = business.stripe_customer_id
            else:
                session_params["customer_email"] = business.studentContactEmail
            session = stripe.checkout.Session.create(**session_params)
            StripeCheckoutAttempt.objects.create(
                business=business,
                checkout_session_id=session.id,
                product_type="marketplace_email_branding",
                plan_or_tier_key="",
                stripe_price_id=price_id or "",
            )
            return Response(
                {"url": session.url, "checkout_url": session.url, "session_id": session.id},
                status=status.HTTP_200_OK,
            )
        except stripe.StripeError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)


class CreateMarketplaceEmailAddonPaymentIntentView(APIView):
    """
    POST: Create addon subscription in default_incomplete; return client_secret for on-site
    Stripe Elements payment. Payment happens on-site (no redirect).
    """

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        gone = stripe_migration_gone_response()
        if gone is not None:
            return gone
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
            items = _obj_get(stripe_sub, "items", {}) or {}
            items_data = _obj_get(items, "data", []) or []
            if items_data:
                first_item = items_data[0]
                price_obj = _obj_get(first_item, "price", {}) or {}
                stripe_price_id = price_obj if isinstance(price_obj, str) else _obj_get(price_obj, "id")

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


def _get_addon_subscription_for_manage(business, addon_type):
    """Return current addon sub for cancel/reactivate; if not found by period, try latest row and sync from Stripe (same as GET addons)."""
    sub = _get_current_addon_subscription(business, addon_type)
    if sub and sub.stripe_subscription_id:
        return sub
    latest = (
        BusinessAddonSubscription.objects.filter(
            business=business,
            addon_type=addon_type,
        )
        .exclude(stripe_subscription_id__isnull=True)
        .exclude(stripe_subscription_id="")
        .order_by("-created_at")
        .first()
    )
    if latest and latest.stripe_subscription_id:
        synced, _ = sync_addon_subscription_from_stripe(
            latest.stripe_subscription_id,
            addon_type=addon_type,
        )
        if synced and synced.status in ("active", "trialing"):
            return synced
    return None


class CancelMarketplaceEmailAddonView(APIView):
    """POST: Set cancel_at_period_end=True in Stripe. DB synced from Stripe (single source of truth)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_addon_subscription_for_manage(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
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
        sub = _get_addon_subscription_for_manage(business, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING)
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
