"""Tests for Stripe Connect webhook at /api/webhooks/stripe-connect/."""
from decimal import Decimal
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

    def _payout_event(self, mock_c, event_type, account, payout):
        mock_c.return_value = SimpleNamespace(
            type=event_type,
            account=account,
            data=SimpleNamespace(object=payout),
        )

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_payout_created_upserts_by_stripe_payout_id(self, mock_c, stripe_client):
        from quickstart.models import Payout

        business = BusinessFactory(stripe_account_id="acct_payout_wh_1")
        stripe_payout = SimpleNamespace(
            id="po_test_created_1",
            amount=12345,
            currency="cad",
            status="pending",
            method="standard",
            arrival_date=1700000000,
            failure_code=None,
            failure_message=None,
        )
        self._payout_event(mock_c, "payout.created", "acct_payout_wh_1", stripe_payout)
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        row = Payout.objects.get(stripe_payout_id="po_test_created_1")
        assert row.business_id == business.businessId
        assert row.amount == Decimal("123.45")
        assert row.currency == "CAD"
        assert row.status == "pending"
        assert row.method == "standard"
        assert row.arrival_date.isoformat() == "2023-11-14"
        assert row.failure_code == ""
        assert row.failure_message == ""

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_payout_paid_updates_existing_row(self, mock_c, stripe_client):
        from quickstart.models import Payout

        business = BusinessFactory(stripe_account_id="acct_payout_wh_2")
        existing = PayoutFactory(
            business=business,
            stripe_payout_id="po_test_paid_1",
            status="pending",
            amount=Decimal("50.00"),
        )
        stripe_payout = SimpleNamespace(
            id="po_test_paid_1",
            amount=5000,
            currency="cad",
            status="paid",
            method="instant",
            arrival_date=1700000000,
            failure_code=None,
            failure_message=None,
        )
        self._payout_event(mock_c, "payout.paid", "acct_payout_wh_2", stripe_payout)
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        existing.refresh_from_db()
        assert existing.status == "paid"
        assert existing.method == "instant"
        assert Payout.objects.filter(stripe_payout_id="po_test_paid_1").count() == 1

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_payout_failed_maps_failure_fields(self, mock_c, stripe_client):
        from quickstart.models import Payout

        BusinessFactory(stripe_account_id="acct_payout_wh_3")
        stripe_payout = SimpleNamespace(
            id="po_test_failed_1",
            amount=2000,
            currency="usd",
            status="failed",
            method="standard",
            arrival_date=None,
            failure_code="could_not_process",
            failure_message="The bank account could not be processed.",
        )
        self._payout_event(mock_c, "payout.failed", "acct_payout_wh_3", stripe_payout)
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        row = Payout.objects.get(stripe_payout_id="po_test_failed_1")
        assert row.status == "failed"
        assert row.failure_code == "could_not_process"
        assert "bank account" in row.failure_message
        assert row.arrival_date is None

    @patch("quickstart.views.webhooks.stripe_webhooks.stripe.Webhook.construct_event")
    def test_payout_canceled_unknown_account_still_200(self, mock_c, stripe_client):
        from quickstart.models import Payout

        stripe_payout = SimpleNamespace(
            id="po_test_canceled_unknown",
            amount=100,
            currency="cad",
            status="canceled",
            method="standard",
            arrival_date=1700000000,
            failure_code=None,
            failure_message=None,
        )
        self._payout_event(
            mock_c, "payout.canceled", "acct_does_not_exist", stripe_payout
        )
        r = stripe_client.post(
            self.URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        assert r.status_code == 200
        assert not Payout.objects.filter(
            stripe_payout_id="po_test_canceled_unknown"
        ).exists()

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
