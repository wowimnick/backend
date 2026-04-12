"""
Email marketing: quota isolation, unsubscribe, addon gate, HTML sanitization.
"""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone
from datetime import timedelta
from django.core.signing import Signer

from rest_framework import status

from quickstart.models import (
    ADDON_TYPE_EMAIL_MARKETING,
    BusinessAddonSubscription,
    BusinessEmailCampaign,
    CampaignEmailSend,
)
from quickstart.tests.factories import BusinessFactory, ContactFactory
from quickstart.utils.marketing_html import sanitize_marketing_html

API = "/api"


def _enable_email_marketing(business, price_id="price_em_test"):
    now = timezone.now()
    BusinessAddonSubscription.objects.create(
        business=business,
        addon_type=ADDON_TYPE_EMAIL_MARKETING,
        stripe_subscription_id=f"sub_em_{business.businessId}",
        stripe_price_id=price_id,
        stripe_customer_id="cus_test",
        status="active",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    business.email_marketing_enabled = True
    business.save(update_fields=["email_marketing_enabled"])


@pytest.mark.django_db
@override_settings(EMAIL_MARKETING_STARTER_PRICE_ID="price_em_test")
def test_marketing_templates_forbidden_without_addon(business_owner_client, business):
    r = business_owner_client.get(f"{API}/my-business/marketing/templates/")
    assert r.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
@override_settings(EMAIL_MARKETING_STARTER_PRICE_ID="price_em_test")
def test_marketing_templates_ok_with_addon(business_owner_client, business):
    _enable_email_marketing(business)
    r = business_owner_client.get(f"{API}/my-business/marketing/templates/")
    assert r.status_code == status.HTTP_200_OK
    assert r.json() == []


@pytest.mark.django_db
def test_sanitize_marketing_html_strips_script():
    dirty = '<p>Hi</p><script>alert(1)</script>'
    clean = sanitize_marketing_html(dirty)
    assert "<script>" not in clean.lower()


@pytest.mark.django_db
@override_settings(EMAIL_MARKETING_STARTER_PRICE_ID="price_em_test")
def test_campaign_detail_other_business_404(business_owner_client, business, other_business):
    _enable_email_marketing(business)
    _enable_email_marketing(other_business, price_id="price_em_test")
    c = BusinessEmailCampaign.objects.create(business=other_business, name="Secret")
    r = business_owner_client.get(f"{API}/my-business/marketing/campaigns/{c.id}/")
    assert r.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_public_unsubscribe_bad_token(api_client):
    r = api_client.get(f"{API}/public/marketing-email/unsubscribe/not-a-real-token/")
    assert r.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_public_unsubscribe_success(api_client, business):
    contact = ContactFactory(business=business, email="sub@example.com", marketing_unsubscribed=False)
    signer = Signer(salt="ce-marketing-unsub-v1")
    token = signer.sign(f"{business.businessId}:{contact.id}")
    r = api_client.get(f"{API}/public/marketing-email/unsubscribe/{token}/")
    assert r.status_code == status.HTTP_200_OK
    contact.refresh_from_db()
    assert contact.marketing_unsubscribed is True


def _signed_resend_post(url, secret, payload_dict):
    raw = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    signed_payload = f"{ts}.{raw.decode('utf-8')}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return Client().post(
        url,
        data=raw,
        content_type="application/json",
        HTTP_RESEND_SIGNATURE=f"t={ts},v={sig}",
    )


@pytest.mark.django_db
@override_settings(RESEND_WEBHOOK_SECRET="whsec_test_marketing")
def test_resend_webhook_updates_campaign_email_send(business):
    campaign = BusinessEmailCampaign.objects.create(business=business, name="News")
    contact = ContactFactory(business=business, email="hit@example.com", marketing_unsubscribed=False)
    send_row = CampaignEmailSend.objects.create(
        campaign=campaign,
        contact=contact,
        to_email="hit@example.com",
        resend_email_id="re_evt_test_1",
        status="sent",
    )
    r = _signed_resend_post(
        f"{API}/webhooks/resend/",
        "whsec_test_marketing",
        {
            "type": "email.delivered",
            "data": {"email_id": "re_evt_test_1", "headers": []},
        },
    )
    assert r.status_code == 200
    send_row.refresh_from_db()
    assert send_row.status == "delivered"


@pytest.mark.django_db
@override_settings(RESEND_WEBHOOK_SECRET="whsec_test_marketing")
def test_resend_webhook_bounce_suppresses_contact(business):
    campaign = BusinessEmailCampaign.objects.create(business=business, name="News")
    contact = ContactFactory(business=business, email="bad@example.com", marketing_unsubscribed=False)
    CampaignEmailSend.objects.create(
        campaign=campaign,
        contact=contact,
        to_email="bad@example.com",
        resend_email_id="re_bounce_1",
        status="sent",
    )
    r = _signed_resend_post(
        f"{API}/webhooks/resend/",
        "whsec_test_marketing",
        {
            "type": "email.bounced",
            "data": {"email_id": "re_bounce_1", "headers": []},
        },
    )
    assert r.status_code == 200
    contact.refresh_from_db()
    assert contact.marketing_unsubscribed is True


# -----------------------------------------------------------------------------
# Email marketing addon: change-tier / cancel / reactivate (Stripe mocked)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailMarketingAddonBillingIntegration:
    @pytest.fixture(autouse=True)
    def _marketing_prices(self, settings):
        settings.EMAIL_MARKETING_STARTER_PRICE_ID = "price_em_starter"
        settings.EMAIL_MARKETING_GROWTH_PRICE_ID = "price_em_growth"

    def test_change_tier_upgrade_200(self, business_owner_client, email_marketing_subscription):
        sub = email_marketing_subscription
        sub.stripe_price_id = "price_em_starter"
        sub.save(update_fields=["stripe_price_id"])

        stripe_sub = SimpleNamespace(
            items=SimpleNamespace(data=[SimpleNamespace(id="si_em_1")]),
        )
        modified = SimpleNamespace(
            items=stripe_sub.items,
            latest_invoice={"status": "paid"},
        )
        with patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.modify",
            return_value=modified,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.proration_after_subscription_item_modify",
            return_value=("success", None, None, None),
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.sync_addon_subscription_from_stripe",
            return_value=(sub, None),
        ) as mock_sync:
            r = business_owner_client.post(
                f"{API}/my-business/addons/email-marketing/change-tier/",
                {"price_id": "price_em_growth"},
                format="json",
            )
        assert r.status_code == status.HTTP_200_OK
        assert r.json().get("success") is True
        mock_sync.assert_called_once()

    def test_change_tier_returns_client_secret_when_proration_needs_payment(
        self, business_owner_client, email_marketing_subscription
    ):
        sub = email_marketing_subscription
        stripe_sub = SimpleNamespace(
            items=SimpleNamespace(data=[SimpleNamespace(id="si_em_1")]),
        )
        with patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.retrieve",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.modify",
            return_value=stripe_sub,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.proration_after_subscription_item_modify",
            return_value=("payment", "pi_secret_em", None, None),
        ):
            r = business_owner_client.post(
                f"{API}/my-business/addons/email-marketing/change-tier/",
                {"price_id": "price_em_growth"},
                format="json",
            )
        assert r.status_code == status.HTTP_200_OK
        body = r.json()
        assert body.get("requires_payment") is True
        assert body.get("client_secret") == "pi_secret_em"

    def test_change_tier_invalid_price_400(self, business_owner_client, email_marketing_subscription):
        r = business_owner_client.post(
            f"{API}/my-business/addons/email-marketing/change-tier/",
            {"price_id": "price_not_configured"},
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST

    def test_change_tier_no_active_sub_404(self, business_owner_client, business):
        """No addon row → manage helper returns nothing."""
        r = business_owner_client.post(
            f"{API}/my-business/addons/email-marketing/change-tier/",
            {"price_id": "price_em_growth"},
            format="json",
        )
        assert r.status_code == status.HTTP_404_NOT_FOUND

    def test_cancel_email_marketing_200(self, business_owner_client, email_marketing_subscription):
        sub = email_marketing_subscription
        sub.cancel_at_period_end = True
        with patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.modify",
            return_value=None,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.sync_addon_subscription_from_stripe",
            return_value=(sub, None),
        ) as mock_sync:
            r = business_owner_client.post(
                f"{API}/my-business/addons/email-marketing/cancel/",
                {},
                format="json",
            )
        assert r.status_code == status.HTTP_200_OK
        assert r.json()["email_marketing"]["cancelAtPeriodEnd"] is True
        mock_sync.assert_called_once()

    def test_reactivate_email_marketing_200(self, business_owner_client, email_marketing_subscription):
        sub = email_marketing_subscription
        sub.cancel_at_period_end = False
        with patch(
            "quickstart.views.business.email_marketing_addon_views.stripe.Subscription.modify",
            return_value=None,
        ), patch(
            "quickstart.views.business.email_marketing_addon_views.sync_addon_subscription_from_stripe",
            return_value=(sub, None),
        ) as mock_sync:
            r = business_owner_client.post(
                f"{API}/my-business/addons/email-marketing/reactivate/",
                {},
                format="json",
            )
        assert r.status_code == status.HTTP_200_OK
        assert r.json()["email_marketing"]["cancelAtPeriodEnd"] is False
        mock_sync.assert_called_once()
