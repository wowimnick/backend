"""Stripe checkout / manage for email marketing ladder (separate from marketplace email branding)."""
import logging

import stripe
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import ADDON_TYPE_EMAIL_MARKETING, BusinessAddonSubscription
from quickstart.services.email_marketing_config import is_valid_marketing_price_id
from quickstart.services.subscription_sync import sync_addon_subscription_from_stripe
from quickstart.utils.permissions import CanManageOwnClasses
from quickstart.views.widget.widget_config_views import (
    _business_can_instant_subscribe,
    _get_addon_subscription_for_manage,
    _get_business_default_payment_method_id,
    _get_business_for_subscription,
    _get_current_addon_subscription,
    _get_subscription_client_secret,
    _obj_get,
)

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def _price_from_request(request):
    return (request.data.get("price_id") or "").strip()


class CreateEmailMarketingAddonCheckoutView(APIView):
    """POST body: price_id (Stripe recurring Price for a configured tier)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = _price_from_request(request)
        if not price_id or not is_valid_marketing_price_id(price_id):
            return Response(
                {"error": "Invalid or unconfigured price_id for email marketing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_EMAIL_MARKETING):
            return Response(
                {"error": "You already have an active email marketing subscription. Use change-tier to switch."},
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
                "success_url": success_url + "?email_marketing=1&session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": cancel_url,
                "client_reference_id": str(business.businessId),
                "subscription_data": {
                    "metadata": {
                        "business_id": str(business.businessId),
                        "addon_type": ADDON_TYPE_EMAIL_MARKETING,
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


class CreateEmailMarketingAddonPaymentIntentView(APIView):
    """POST body: price_id — on-site Elements subscription."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = _price_from_request(request)
        if not price_id or not is_valid_marketing_price_id(price_id):
            return Response(
                {"error": "Invalid or unconfigured price_id for email marketing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_EMAIL_MARKETING):
            return Response(
                {"error": "You already have an active email marketing subscription."},
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
                addon_type=ADDON_TYPE_EMAIL_MARKETING,
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
                    "addon_type": ADDON_TYPE_EMAIL_MARKETING,
                },
            )
            client_secret = _get_subscription_client_secret(stripe_sub)
            if not client_secret:
                return Response(
                    {"error": "Could not create payment form. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            items = _obj_get(stripe_sub, "items", {}) or {}
            items_data = _obj_get(items, "data", []) or []
            stripe_price_id = None
            if items_data:
                first_item = items_data[0]
                price_obj = _obj_get(first_item, "price", {}) or {}
                stripe_price_id = price_obj if isinstance(price_obj, str) else _obj_get(price_obj, "id")
            BusinessAddonSubscription.objects.update_or_create(
                stripe_subscription_id=stripe_sub.id,
                defaults={
                    "business": business,
                    "addon_type": ADDON_TYPE_EMAIL_MARKETING,
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


class InstantSubscribeEmailMarketingAddonView(APIView):
    """POST body: price_id — charge saved default payment method."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = _price_from_request(request)
        if not price_id or not is_valid_marketing_price_id(price_id):
            return Response(
                {"error": "Invalid or unconfigured price_id for email marketing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        business = _get_business_for_subscription(request.user)
        if _get_current_addon_subscription(business, ADDON_TYPE_EMAIL_MARKETING):
            return Response(
                {"error": "You already have an active email marketing subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        pm_id = _get_business_default_payment_method_id(business)
        if not pm_id:
            return Response(
                {"error": "No saved payment method.", "can_instant": False},
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
                    "addon_type": ADDON_TYPE_EMAIL_MARKETING,
                },
            )
        except stripe.StripeError as e:
            err_msg = str(e).lower()
            if "decline" in err_msg or "card" in err_msg or "payment" in err_msg:
                return Response(
                    {
                        "error": "Your saved card was declined.",
                        "can_instant": False,
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )
            return Response({"error": str(e), "can_instant": False}, status=status.HTTP_400_BAD_REQUEST)

        synced, _ = sync_addon_subscription_from_stripe(
            stripe_sub.id,
            subscription_obj=stripe_sub,
            addon_type=ADDON_TYPE_EMAIL_MARKETING,
        )
        if not synced:
            return Response(
                {"error": "Subscription created but could not sync. Please refresh."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "email_marketing": {
                    "active": True,
                    "currentPeriodEnd": (
                        synced.current_period_end.isoformat() if synced.current_period_end else None
                    ),
                    "cancelAtPeriodEnd": synced.cancel_at_period_end,
                }
            },
            status=status.HTTP_200_OK,
        )


class CancelEmailMarketingAddonView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_addon_subscription_for_manage(business, ADDON_TYPE_EMAIL_MARKETING)
        if not sub or not sub.stripe_subscription_id:
            return Response({"error": "No active email marketing subscription."}, status=404)
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
        except stripe.StripeError as e:
            logger.warning("email marketing cancel_at_period_end: %s", e)
            return Response({"error": "Could not update cancellation."}, status=502)
        synced, _ = sync_addon_subscription_from_stripe(
            sub.stripe_subscription_id, addon_type=ADDON_TYPE_EMAIL_MARKETING
        )
        sub = synced or sub
        return Response(
            {
                "email_marketing": {
                    "active": True,
                    "currentPeriodEnd": sub.current_period_end.isoformat() if sub.current_period_end else None,
                    "cancelAtPeriodEnd": sub.cancel_at_period_end,
                }
            }
        )


class ReactivateEmailMarketingAddonView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        business = _get_business_for_subscription(request.user)
        sub = _get_addon_subscription_for_manage(business, ADDON_TYPE_EMAIL_MARKETING)
        if not sub or not sub.stripe_subscription_id:
            return Response({"error": "No active email marketing subscription."}, status=404)
        try:
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=False)
        except stripe.StripeError as e:
            logger.warning("email marketing reactivate: %s", e)
            return Response({"error": "Could not reactivate."}, status=502)
        synced, _ = sync_addon_subscription_from_stripe(
            sub.stripe_subscription_id, addon_type=ADDON_TYPE_EMAIL_MARKETING
        )
        sub = synced or sub
        return Response(
            {
                "email_marketing": {
                    "active": True,
                    "currentPeriodEnd": sub.current_period_end.isoformat() if sub.current_period_end else None,
                    "cancelAtPeriodEnd": sub.cancel_at_period_end,
                }
            }
        )


class ChangeEmailMarketingTierView(APIView):
    """POST body: price_id — swap subscription item (prorated)."""

    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def post(self, request, *args, **kwargs):
        price_id = _price_from_request(request)
        if not price_id or not is_valid_marketing_price_id(price_id):
            return Response({"error": "Invalid price_id."}, status=400)
        business = _get_business_for_subscription(request.user)
        sub = _get_addon_subscription_for_manage(business, ADDON_TYPE_EMAIL_MARKETING)
        if not sub or not sub.stripe_subscription_id:
            return Response({"error": "No active email marketing subscription."}, status=404)
        try:
            stripe_sub = stripe.Subscription.retrieve(
                sub.stripe_subscription_id,
                expand=["items.data.price"],
            )
            items = _obj_get(_obj_get(stripe_sub, "items", {}) or {}, "data", []) or []
            if not items:
                return Response({"error": "Subscription has no items."}, status=400)
            item_id = _obj_get(items[0], "id")
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                items=[{"id": item_id, "price": price_id}],
                proration_behavior="create_prorations",
            )
        except stripe.StripeError as e:
            return Response({"error": str(e)}, status=502)
        synced, _ = sync_addon_subscription_from_stripe(
            sub.stripe_subscription_id, addon_type=ADDON_TYPE_EMAIL_MARKETING
        )
        return Response({"success": True, "subscription_id": sub.stripe_subscription_id})
