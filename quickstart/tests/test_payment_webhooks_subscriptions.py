"""
Subscription-related Stripe webhook behavior (idempotency, widget SaaS paths).
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.models import ProcessedStripeEvent, WidgetSubscription
from quickstart.tests.factories import BusinessFactory

API = "/api"


@pytest.mark.django_db
class TestSubscriptionWebhookIdempotency:
    @patch("quickstart.payments.views.send_widget_subscription_payment_failed_email")
    @patch("quickstart.payments.views.stripe.Subscription.retrieve")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_invoice_payment_failed_duplicate_event_id(
        self, mock_construct, mock_sub_retrieve, mock_email, api_client
    ):
        business = BusinessFactory()
        WidgetSubscription.objects.create(
            business=business,
            plan_id="growth",
            status="active",
            stripe_subscription_id="sub_wh_idem",
        )
        invoice = SimpleNamespace(id="in_wh_pf_idem", subscription="sub_wh_idem")
        event = SimpleNamespace(
            type="invoice.payment_failed",
            id="evt_invoice_pf_idem",
            data=SimpleNamespace(object=invoice),
        )
        mock_construct.return_value = event
        mock_sub_retrieve.return_value = SimpleNamespace(
            metadata={"business_id": str(business.businessId), "plan_id": "growth"}
        )

        def post_wh():
            return api_client.post(
                f"{API}/payments/webhook/",
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig_test",
            )

        assert post_wh().status_code == status.HTTP_200_OK
        assert post_wh().status_code == status.HTTP_200_OK
        assert ProcessedStripeEvent.objects.filter(event_id="evt_invoice_pf_idem").count() == 1
        assert mock_email.call_count == 1


@pytest.mark.django_db
def test_sync_widget_subscriptions_command_handles_empty():
    from io import StringIO
    from django.core.management import call_command

    out = StringIO()
    call_command("sync_widget_subscriptions_from_stripe", limit=0, stdout=out)
    body = out.getvalue().lower()
    assert "complete" in body
