"""Native Stripe Connect payout settings, balance, and manual/instant payouts."""

import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from quickstart.models import BusinessInfo, Payout
from quickstart.utils.permissions import CanManageOwnBusinessProfile

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

VALID_INTERVALS = {"manual", "daily", "weekly", "monthly"}
WEEKDAY_ANCHORS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


def _get_business(user):
    business = (
        BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user, staff_members__status="accepted")
        )
        .distinct()
        .first()
    )
    return business


def _require_connect(business):
    if not business or not business.stripe_account_id:
        return Response(
            {"error": "Connect a bank account before managing payouts."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


class ConnectPayoutSettingsView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]

    def get(self, request):
        business = _get_business(request.user)
        return Response(
            {
                "payout_interval": business.payout_interval if business else "daily",
                "payout_weekly_anchor": getattr(business, "payout_weekly_anchor", "monday")
                if business
                else "monday",
                "payout_monthly_anchor": getattr(business, "payout_monthly_anchor", 1)
                if business
                else 1,
                "instant_payouts_enabled": bool(
                    getattr(business, "instant_payouts_enabled", False)
                )
                if business
                else False,
                "charges_enabled": (business.stripe_account_status == "active")
                if business
                else False,
            }
        )

    def patch(self, request):
        business = _get_business(request.user)
        err = _require_connect(business)
        if err:
            return err

        interval = (request.data.get("payout_interval") or business.payout_interval or "daily").lower()
        if interval not in VALID_INTERVALS:
            return Response({"error": "Invalid payout_interval"}, status=status.HTTP_400_BAD_REQUEST)

        weekly = (request.data.get("payout_weekly_anchor") or business.payout_weekly_anchor or "monday").lower()
        if weekly not in WEEKDAY_ANCHORS:
            weekly = "monday"
        monthly = int(request.data.get("payout_monthly_anchor") or business.payout_monthly_anchor or 1)
        monthly = max(1, min(31, monthly))
        instant = bool(request.data.get("instant_payouts_enabled", business.instant_payouts_enabled))

        schedule = {"interval": interval}
        if interval == "weekly":
            schedule["weekly_anchor"] = weekly
        if interval == "monthly":
            schedule["monthly_anchor"] = monthly

        try:
            stripe.Account.modify(
                business.stripe_account_id,
                settings={"payouts": {"schedule": schedule}},
            )
        except stripe.StripeError as e:
            logger.error("Stripe payout schedule update failed: %s", e)
            return Response({"error": str(e.user_message or e)}, status=status.HTTP_400_BAD_REQUEST)

        business.payout_interval = interval
        business.payout_weekly_anchor = weekly
        business.payout_monthly_anchor = monthly
        business.instant_payouts_enabled = instant
        business.save(
            update_fields=[
                "payout_interval",
                "payout_weekly_anchor",
                "payout_monthly_anchor",
                "instant_payouts_enabled",
            ]
        )
        return self.get(request)


class ConnectPayoutBalanceView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]

    def get(self, request):
        business = _get_business(request.user)
        err = _require_connect(business)
        if err:
            return err
        try:
            balance = stripe.Balance.retrieve(stripe_account=business.stripe_account_id)
        except stripe.StripeError as e:
            return Response({"error": str(e.user_message or e)}, status=status.HTTP_400_BAD_REQUEST)

        def _sum(bucket):
            return {
                item.get("currency", business.currency).upper(): Decimal(item.get("amount", 0)) / 100
                for item in (bucket or [])
            }

        available = _sum(getattr(balance, "available", []))
        pending = _sum(getattr(balance, "pending", []))
        instant_available = _sum(getattr(balance, "instant_available", []) or [])
        currency = (business.currency or "CAD").upper()
        return Response(
            {
                "currency": currency,
                "available": float(available.get(currency, Decimal("0.00"))),
                "pending": float(pending.get(currency, Decimal("0.00"))),
                "instant_available": float(instant_available.get(currency, Decimal("0.00"))),
                "payout_interval": business.payout_interval,
                "instant_payouts_enabled": business.instant_payouts_enabled,
            }
        )


class ConnectPayoutCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]

    def post(self, request):
        business = _get_business(request.user)
        err = _require_connect(business)
        if err:
            return err

        try:
            amount = Decimal(str(request.data.get("amount", "0")))
        except Exception:
            return Response({"error": "Invalid amount"}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"error": "Amount must be greater than 0"}, status=status.HTTP_400_BAD_REQUEST)

        method = (request.data.get("method") or "standard").lower()
        if method == "instant" and not business.instant_payouts_enabled:
            return Response(
                {"error": "Instant payouts are not enabled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        params = {
            "amount": int(amount * 100),
            "currency": (business.currency or "cad").lower(),
        }
        if method == "instant":
            params["method"] = "instant"

        try:
            payout = stripe.Payout.create(**params, stripe_account=business.stripe_account_id)
        except stripe.StripeError as e:
            return Response({"error": str(e.user_message or e)}, status=status.HTTP_400_BAD_REQUEST)

        row, _ = Payout.objects.update_or_create(
            stripe_payout_id=payout.id,
            defaults={
                "business": business,
                "amount": amount,
                "currency": (business.currency or "CAD").upper(),
                "status": payout.status,
                "method": method,
                "arrival_date": None,
            },
        )
        return Response(
            {
                "id": str(row.id),
                "stripe_payout_id": payout.id,
                "amount": str(amount),
                "status": payout.status,
                "method": method,
            },
            status=status.HTTP_201_CREATED,
        )


class ConnectPayoutExternalAccountsView(APIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]

    def get(self, request):
        business = _get_business(request.user)
        err = _require_connect(business)
        if err:
            return err
        try:
            accounts = stripe.Account.list_external_accounts(
                business.stripe_account_id, object="bank_account", limit=20
            )
            cards = stripe.Account.list_external_accounts(
                business.stripe_account_id, object="card", limit=20
            )
        except stripe.StripeError as e:
            return Response({"error": str(e.user_message or e)}, status=status.HTTP_400_BAD_REQUEST)

        def _serialize(item):
            return {
                "id": item.id,
                "object": item.object,
                "last4": getattr(item, "last4", None),
                "bank_name": getattr(item, "bank_name", None),
                "brand": getattr(item, "brand", None),
                "available_payout_methods": list(getattr(item, "available_payout_methods", []) or []),
                "default_for_currency": getattr(item, "default_for_currency", False),
            }

        return Response(
            {
                "bank_accounts": [_serialize(i) for i in accounts.data],
                "cards": [_serialize(i) for i in cards.data],
            }
        )
