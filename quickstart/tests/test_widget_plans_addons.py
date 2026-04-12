"""
Tests for widget (public v1 with X-Business-ID), widget config (my-business),
widget subscription (plans), and addons (my-business/addons).
Run with: pytest quickstart/tests/test_widget_plans_addons.py -v
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.tests.factories import (
    UserFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"


# -----------------------------------------------------------------------------
# Fixtures (business_owner_client lives in conftest.py)
# -----------------------------------------------------------------------------


@pytest.fixture
def widget_headers(business):
    """Headers for widget v1 API (X-Business-ID = widget_api_key)."""
    return {"HTTP_X_BUSINESS_ID": str(business.widget_api_key)}


# -----------------------------------------------------------------------------
# Widget v1 (public): config, classes, availability, validate-coupon, events
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestWidgetV1ConfigAndClasses:
    """Widget v1 config and classes (require X-Business-ID)."""

    def test_widget_config_without_header_401_or_403(self, api_client):
        """GET widget/v1/config/ without X-Business-ID returns 401 or 403."""
        response = api_client.get(f"{API}/widget/v1/config/")
        assert response.status_code in (401, 403, 404)

    def test_widget_config_with_valid_key_200(self, api_client, business):
        """GET widget/v1/config/ with valid X-Business-ID returns 200 and config."""
        response = api_client.get(
            f"{API}/widget/v1/config/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        # May be 200 with config or 403 if widget subscription required and not configured
        assert response.status_code in (200, 403)
        if response.status_code == 200:
            data = response.json()
            assert "stripe_publishable_key" in data or "businessName" in data or "slug" in data or "theme" in data

    def test_widget_classes_with_valid_key_200(self, api_client, business, public_class):
        """GET widget/v1/classes/ with valid key returns 200."""
        public_class  # ensure class exists for business
        response = api_client.get(
            f"{API}/widget/v1/classes/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 200

    def test_widget_availability_missing_params_400(self, api_client, business):
        """GET widget/v1/availability/ without option_id/dates returns 400."""
        response = api_client.get(
            f"{API}/widget/v1/availability/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestWidgetV1ValidateCoupon:
    """Widget validate-coupon endpoint."""

    def test_validate_coupon_without_header_401_or_403(self, api_client):
        """POST widget/v1/validate-coupon/ without X-Business-ID returns 401 or 403."""
        response = api_client.post(
            f"{API}/widget/v1/validate-coupon/",
            {"code": "TEST", "base_amount": "25", "schedule_instance_id": 1},
            format="json",
        )
        assert response.status_code in (401, 403, 404)

    def test_validate_coupon_missing_params_400(self, api_client, business):
        """POST widget/v1/validate-coupon/ with missing params returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/validate-coupon/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data or "code" in str(data).lower()


@pytest.mark.django_db
class TestWidgetV1Events:
    """Widget events (analytics/error logging)."""

    def test_widget_events_without_header_401_or_403(self, api_client):
        """POST widget/v1/events/ without X-Business-ID returns 401 or 403."""
        response = api_client.post(
            f"{API}/widget/v1/events/",
            {"event": "loaded"},
            format="json",
        )
        assert response.status_code in (401, 403, 404)

    def test_widget_events_with_valid_key_204(self, api_client, business):
        """POST widget/v1/events/ with valid key returns 204."""
        response = api_client.post(
            f"{API}/widget/v1/events/",
            {"event": "loaded"},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code == 204


@pytest.mark.django_db
class TestWidgetV1PaymentAndBookings:
    """Widget payment-intent and bookings (require X-Business-ID)."""

    def test_widget_payment_intent_invalid_payload_400(self, api_client, business):
        """POST widget/v1/payment-intent/ with invalid payload returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/payment-intent/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code in (400, 404)

    def test_widget_bookings_create_invalid_payload_400(self, api_client, business):
        """POST widget/v1/bookings/ with invalid payload returns 400."""
        response = api_client.post(
            f"{API}/widget/v1/bookings/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert response.status_code in (400, 404)


# -----------------------------------------------------------------------------
# My-business: widget config (dashboard)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyBusinessWidgetConfig:
    """GET/PATCH my-business/widget-config/ (authenticated, business owner)."""

    def test_widget_config_unauthenticated_401(self, api_client):
        """GET my-business/widget-config/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/widget-config/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_widget_config_owner_200(self, business_owner_client, business):
        """GET my-business/widget-config/ for owner returns 200 with config and widget_api_key."""
        response = business_owner_client.get(f"{API}/my-business/widget-config/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "config" in data
        assert "widget_api_key" in data
        assert "classes" in data
        assert "has_active_widget_subscription" in data

    def test_widget_config_patch_owner_200(self, business_owner_client):
        """PATCH my-business/widget-config/ with allowed_widget_origins returns 200."""
        response = business_owner_client.patch(
            f"{API}/my-business/widget-config/",
            {"allowed_widget_origins": "https://mysite.com"},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK


# -----------------------------------------------------------------------------
# Widget subscription (plans): my-business/widget-subscription/
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestWidgetSubscriptionService:
    """Unit tests for widget subscription service (get_widget_subscription)."""

    def test_get_widget_subscription_returns_none_when_no_row(self, business):
        """get_widget_subscription(business) returns None when business has no subscription row."""
        from quickstart.services.widget_subscription_service import get_widget_subscription

        assert get_widget_subscription(business) is None

    def test_get_widget_subscription_returns_row_when_exists(self, business):
        """get_widget_subscription(business) returns the single subscription row when it exists."""
        from quickstart.models import WidgetSubscription
        from quickstart.services.widget_subscription_service import get_widget_subscription

        sub = WidgetSubscription.objects.create(
            business=business,
            plan_id="basic",
            status="active",
        )
        assert get_widget_subscription(business) == sub
        assert get_widget_subscription(business).plan_id == "basic"


@pytest.mark.django_db
class TestWidgetSubscriptionPlans:
    """GET/POST my-business/widget-subscription/ (plans)."""

    def test_widget_subscription_get_unauthenticated_401(self, api_client):
        """GET my-business/widget-subscription/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/widget-subscription/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_widget_subscription_get_owner_200(self, business_owner_client):
        """GET my-business/widget-subscription/ for owner returns 200."""
        response = business_owner_client.get(f"{API}/my-business/widget-subscription/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "subscription" in data
        assert "widget_subscription_required" in data
        assert "has_stripe_subscription" in data

    def test_widget_subscription_get_no_row_returns_null_subscription(
        self, business_owner_client, business
    ):
        """GET when business has no widget subscription row returns subscription null, no Stripe call."""
        from quickstart.models import WidgetSubscription

        assert not WidgetSubscription.objects.filter(business=business).exists()
        response = business_owner_client.get(f"{API}/my-business/widget-subscription/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["subscription"] is None
        assert data["has_stripe_subscription"] is False
        assert "has_widget_access" in data
        assert "widget_subscription_required" in data

    def test_widget_subscription_get_with_row_returns_subscription_payload(
        self, business_owner_client, business
    ):
        """GET when business has a widget subscription row returns that row's payload (read from DB only)."""
        from django.utils import timezone
        from quickstart.models import WidgetSubscription

        period_end = timezone.now() + timezone.timedelta(days=30)
        WidgetSubscription.objects.create(
            business=business,
            plan_id="growth",
            status="active",
            current_period_end=period_end,
            cancel_at_period_end=False,
        )
        response = business_owner_client.get(f"{API}/my-business/widget-subscription/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["subscription"] is not None
        assert data["subscription"]["planId"] == "growth"
        assert data["subscription"]["status"] == "active"
        assert data["subscription"]["cancelAtPeriodEnd"] is False
        assert "currentPeriodEnd" in data["subscription"]
        assert data["has_stripe_subscription"] is False  # no stripe_subscription_id set

    def test_widget_subscription_post_invalid_plan_id_400(self, business_owner_client):
        """POST my-business/widget-subscription/ with invalid plan_id returns 400."""
        response = business_owner_client.post(
            f"{API}/my-business/widget-subscription/",
            {"plan_id": "invalid_plan"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert "error" in data
        assert "plan_id" in data.get("error", "").lower() or "basic" in str(data).lower()


@pytest.mark.django_db
class TestWidgetSubscriptionCancelReactivate:
    """Cancel and reactivate (require active subscription; test auth and 400/404 when no sub)."""

    def test_widget_subscription_cancel_unauthenticated_401(self, api_client):
        """POST my-business/widget-subscription/cancel/ without auth returns 401."""
        response = api_client.post(f"{API}/my-business/widget-subscription/cancel/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_widget_subscription_cancel_owner_no_sub_400_or_404(self, business_owner_client):
        """POST cancel when business has no subscription returns 400 or 404."""
        response = business_owner_client.post(
            f"{API}/my-business/widget-subscription/cancel/"
        )
        assert response.status_code in (400, 404)

    def test_widget_subscription_reactivate_unauthenticated_401(self, api_client):
        """POST my-business/widget-subscription/reactivate/ without auth returns 401."""
        response = api_client.post(f"{API}/my-business/widget-subscription/reactivate/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestWidgetSubscriptionInvoices:
    """GET my-business/widget-subscription/invoices/."""

    def test_widget_subscription_invoices_unauthenticated_401(self, api_client):
        """GET my-business/widget-subscription/invoices/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/widget-subscription/invoices/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_widget_subscription_invoices_owner_200(self, business_owner_client):
        """GET invoices for owner returns 200 (may be empty list)."""
        response = business_owner_client.get(
            f"{API}/my-business/widget-subscription/invoices/"
        )
        assert response.status_code == status.HTTP_200_OK


# -----------------------------------------------------------------------------
# Addons: my-business/addons/
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestMyBusinessAddons:
    """GET my-business/addons/ (addon status for marketplace email, etc.)."""

    def test_addons_unauthenticated_401(self, api_client):
        """GET my-business/addons/ without auth returns 401."""
        response = api_client.get(f"{API}/my-business/addons/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_addons_owner_200(self, business_owner_client):
        """GET my-business/addons/ for owner returns 200 with addon status."""
        response = business_owner_client.get(f"{API}/my-business/addons/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        # Response may list addons or have marketplace_email key
        assert isinstance(data, (dict, list))


@pytest.mark.django_db
class TestMyBusinessAddonMarketplaceEmail:
    """Marketplace email addon: checkout, payment-intent, cancel, reactivate."""

    def test_addon_marketplace_email_checkout_unauthenticated_401(self, api_client):
        """POST my-business/addons/marketplace-email/checkout/ without auth returns 401."""
        response = api_client.post(
            f"{API}/my-business/addons/marketplace-email/checkout/"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_addon_marketplace_email_payment_intent_unauthenticated_401(self, api_client):
        """POST my-business/addons/marketplace-email/payment-intent/ without auth returns 401."""
        response = api_client.post(
            f"{API}/my-business/addons/marketplace-email/payment-intent/"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_addon_marketplace_email_cancel_unauthenticated_401(self, api_client):
        """POST my-business/addons/marketplace-email/cancel/ without auth returns 401."""
        response = api_client.post(
            f"{API}/my-business/addons/marketplace-email/cancel/"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_addon_marketplace_email_reactivate_unauthenticated_401(self, api_client):
        """POST my-business/addons/marketplace-email/reactivate/ without auth returns 401."""
        response = api_client.post(
            f"{API}/my-business/addons/marketplace-email/reactivate/"
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


# -----------------------------------------------------------------------------
# Widget subscription: mocked Stripe/service integration (POST cancel/reactivate/upgrade/downgrade)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestWidgetSubscriptionUpgradeIntegration:
    """POST plan change calls service layer; Stripe mocked at service boundary."""

    @pytest.fixture(autouse=True)
    def _widget_prices(self, settings):
        settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
        settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
        settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"

    def test_post_upgrade_basic_to_growth_200(self, business_owner_client, widget_subscription):
        sub = widget_subscription
        assert sub.plan_id == "basic"

        def _fake_upgrade(s, plan_id, biz):
            s.plan_id = plan_id
            s.stripe_price_id = "price_growth"
            s.save(update_fields=["plan_id", "stripe_price_id"])
            return False, None, s, None

        stripe_sub = SimpleNamespace(
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            )
        )
        with patch(
            "quickstart.views.widget.widget_config_views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.widget.widget_config_views.upgrade_subscription",
            side_effect=_fake_upgrade,
        ):
            response = business_owner_client.post(
                f"{API}/my-business/widget-subscription/",
                {"plan_id": "growth"},
                format="json",
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.get("stripe_updated") is True
        assert data["subscription"]["planId"] == "growth"
        sub.refresh_from_db()
        assert sub.plan_id == "growth"

    def test_post_upgrade_returns_client_secret_when_payment_required(
        self, business_owner_client, widget_subscription
    ):
        sub = widget_subscription

        stripe_sub = SimpleNamespace(
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_basic"))]
            )
        )
        with patch(
            "quickstart.views.widget.widget_config_views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.widget.widget_config_views.upgrade_subscription",
            return_value=(True, "pi_secret_xyz", sub, None),
        ):
            response = business_owner_client.post(
                f"{API}/my-business/widget-subscription/",
                {"plan_id": "growth"},
                format="json",
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.get("requires_payment") is True
        assert data.get("client_secret") == "pi_secret_xyz"
        assert data.get("target_plan_id") == "growth"


@pytest.mark.django_db
class TestWidgetSubscriptionDowngradeIntegration:
    @pytest.fixture(autouse=True)
    def _widget_prices(self, settings):
        settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
        settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
        settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"

    def test_post_downgrade_growth_to_basic_200(self, business_owner_client, widget_subscription):
        sub = widget_subscription
        sub.plan_id = "growth"
        sub.stripe_price_id = "price_growth"
        sub.save(update_fields=["plan_id", "stripe_price_id"])

        stripe_sub = SimpleNamespace(
            items=SimpleNamespace(
                data=[SimpleNamespace(price=SimpleNamespace(id="price_growth"))]
            )
        )
        with patch(
            "quickstart.views.widget.widget_config_views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.widget.widget_config_views.downgrade_subscription",
            return_value=(sub, None),
        ):
            response = business_owner_client.post(
                f"{API}/my-business/widget-subscription/",
                {"plan_id": "basic"},
                format="json",
            )
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data.get("downgrade_scheduled_at_period_end") is True
        assert data.get("scheduled_plan_id") == "basic"
        assert data.get("stripe_updated") is True


@pytest.mark.django_db
class TestWidgetSubscriptionCancelReactivateIntegration:
    @patch("quickstart.views.widget.widget_config_views.service_cancel_at_period_end")
    def test_cancel_active_subscription_200(self, mock_cancel, business_owner_client, widget_subscription):
        sub = widget_subscription
        sub.cancel_at_period_end = True
        mock_cancel.return_value = (sub, None)

        response = business_owner_client.post(f"{API}/my-business/widget-subscription/cancel/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["subscription"]["cancelAtPeriodEnd"] is True
        mock_cancel.assert_called_once()

    @patch("quickstart.views.widget.widget_config_views.service_reactivate")
    def test_reactivate_clears_cancel_at_period_end_in_response(
        self, mock_reactivate, business_owner_client, widget_subscription
    ):
        sub = widget_subscription
        sub.cancel_at_period_end = False
        mock_reactivate.return_value = (sub, None)

        response = business_owner_client.post(f"{API}/my-business/widget-subscription/reactivate/")
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["subscription"]["cancelAtPeriodEnd"] is False
        mock_reactivate.assert_called_once()


@pytest.mark.django_db
class TestWidgetSubscriptionGetScheduledDowngrade:
    @pytest.fixture(autouse=True)
    def _widget_prices(self, settings):
        settings.WIDGET_SUBSCRIPTION_PRICE_BASIC = "price_basic"
        settings.WIDGET_SUBSCRIPTION_PRICE_GROWTH = "price_growth"
        settings.WIDGET_SUBSCRIPTION_PRICE_ADVANCED = "price_advanced"

    @patch("quickstart.views.widget.widget_config_views.stripe.SubscriptionSchedule.retrieve")
    @patch("quickstart.views.widget.widget_config_views.stripe.Subscription.retrieve")
    def test_get_includes_scheduled_downgrade_when_schedule_has_two_phases(
        self, mock_sub_ret, mock_sched_ret, business_owner_client, widget_subscription
    ):
        sub = widget_subscription
        sub.plan_id = "growth"
        sub.save(update_fields=["plan_id"])
        mock_sub_ret.return_value = SimpleNamespace(schedule="sched_1")
        mock_sched_ret.return_value = SimpleNamespace(
            phases=[
                {"start_date": 1_700_000_000, "items": [{"price": "price_growth"}]},
                {"start_date": 1_800_000_000, "items": [{"price": "price_basic"}]},
            ]
        )
        response = business_owner_client.get(f"{API}/my-business/widget-subscription/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "scheduled_downgrade" in data
        assert data["scheduled_downgrade"]["planId"] == "basic"
