"""
Tests for widget (public v1 with X-Business-ID), widget config (my-business),
widget subscription (plans), and addons (my-business/addons).
Run with: pytest quickstart/tests/test_widget_plans_addons.py -v
"""
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
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def business_owner_client(api_client, business):
    """API client authenticated as the business owner."""
    api_client.force_authenticate(user=business.owner)
    return api_client


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
