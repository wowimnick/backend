"""Tests for Stripe Connect webhook at /api/webhooks/stripe-connect/."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import Client

from quickstart.tests.factories import BusinessFactory, PayoutFactory


@pytest.fixture(autouse=True)
def stripe_connect_webhook_secret(settings):
    settings.STRIPE_CONNECT_WEBHOOK_SECRET = "whsec_test_connect"


@pytest.fixture
def stripe_client():
    return Client()


@pytest.mark.django_db
class TestStripeConnectWebhook:
    URL = "/api/webhooks/stripe-connect/"

    def test_get_ping_200(self, stripe_client):
        r = stripe_client.get(self.URL)
        assert r.status_code == 200
        assert b"active" in r.content.lower()

    def test_post_without_signature_uses_construct_event_failure(self, stripe_client):
        with patch(
            "quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event"
        ) as mock_c:
            mock_c.side_effect = ValueError("no signature")
            r = stripe_client.post(
                self.URL,
                data=b"{}",
                content_type="application/json",
            )
        assert r.status_code == 400

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_transfer_updates_existing_payout_to_paid(self, mock_c, stripe_client):
        payout = PayoutFactory()
        transfer = SimpleNamespace(
            id=payout.stripe_transfer_id,
            amount=int(payout.amount * 100),
            currency=payout.currency.lower(),
            created=1700000000,
            metadata=SimpleNamespace(),
        )
        mock_c.return_value = SimpleNamespace(
            type="transfer.created",
            data=SimpleNamespace(object=transfer),
        )
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        payout.refresh_from_db()
        assert payout.status == "paid"

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_account_updated_updates_business_status(self, mock_c, stripe_client):
        business = BusinessFactory(
            stripe_account_id="acct_test_webhook_1",
            stripe_account_status="incomplete",
        )
        account = SimpleNamespace(
            id="acct_test_webhook_1",
            charges_enabled=True,
            payouts_enabled=True,
            details_submitted=True,
            disabled_reason=None,
            requirements=SimpleNamespace(currently_due=[]),
        )
        mock_c.return_value = SimpleNamespace(
            type="account.updated",
            data=SimpleNamespace(object=account),
        )
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        business.refresh_from_db()
        assert business.stripe_account_status == "active"

    @patch(
        "quickstart.views.webhooks.stripe_webhooks.stripe.Account.retrieve",
        return_value=SimpleNamespace(
            id="acct_cap_test",
            charges_enabled=False,
            payouts_enabled=False,
            details_submitted=True,
            disabled_reason="requirements.past_due",
            requirements=SimpleNamespace(currently_due=["identity.document"]),
        ),
    )
    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_capability_updated_refetches_account(
        self, mock_c, _mock_retrieve, stripe_client
    ):
        business = BusinessFactory(
            stripe_account_id="acct_cap_test",
            stripe_account_status="active",
        )
        cap = SimpleNamespace(account="acct_cap_test")
        mock_c.return_value = SimpleNamespace(
            type="capability.updated",
            data=SimpleNamespace(object=cap),
        )
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        business.refresh_from_db()
        assert business.stripe_account_status == "restricted"

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_unknown_event_type_still_200(self, mock_c, stripe_client):
        mock_c.return_value = SimpleNamespace(
            type="customer.created",
            data=SimpleNamespace(object=SimpleNamespace(id="cus_x")),
        )
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
