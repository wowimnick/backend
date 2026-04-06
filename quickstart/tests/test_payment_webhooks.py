"""
Tests for ProcessBookingWebhook (Stripe booking / gift-card payments).
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.models import GiftCard, Payment
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ContactFactory,
    PaymentFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"


def _evt_payment_intent_succeeded(pi_obj):
    return SimpleNamespace(
        type="payment_intent.succeeded",
        data=SimpleNamespace(object=pi_obj),
        id="evt_test_pi_succeeded",
    )


def _evt_payment_intent_failed(pi_obj):
    return SimpleNamespace(
        type="payment_intent.payment_failed",
        data=SimpleNamespace(object=pi_obj),
        id="evt_test_pi_failed",
    )


@pytest.mark.django_db
class TestProcessBookingWebhook:
    """POST /api/payments/webhook/ with mocked stripe.Webhook.construct_event."""

    def _post_webhook(self, api_client):
        return api_client.post(
            f"{API}/payments/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )

    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_invalid_signature_400(self, mock_construct, api_client):
        mock_construct.side_effect = ValueError("bad sig")
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_400_BAD_REQUEST

    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_gift_card_purchase_creates_gift_card(
        self, mock_construct, mock_send_email, api_client
    ):
        pi_id = "pi_gift_webhook_test"
        pi = SimpleNamespace(
            id=pi_id,
            amount=5000,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "recv@example.com",
                "recipient_name": "Recv",
                "sender_name": "Sender",
                "message": "Hi",
                "is_scheduled": "False",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        gc = GiftCard.objects.filter(stripe_payment_intent_id=pi_id).first()
        assert gc is not None
        assert gc.current_balance == Decimal("50.00")
        mock_send_email.assert_called_once()

    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_gift_card_scheduled_defers_email(
        self, mock_construct, mock_send_email, api_client
    ):
        pi_id = "pi_gift_scheduled_test"
        pi = SimpleNamespace(
            id=pi_id,
            amount=2500,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "r2@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "is_scheduled": "True",
                "scheduled_date": "2030-01-01",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        gc = GiftCard.objects.get(stripe_payment_intent_id=pi_id)
        assert gc.is_scheduled is True
        mock_send_email.assert_not_called()

    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_gift_card_idempotent_second_webhook(
        self, mock_construct, mock_send_email, api_client
    ):
        pi_id = "pi_gift_idem_test"
        GiftCard.objects.create(
            initial_amount=Decimal("10"),
            current_balance=Decimal("10"),
            recipient_email="x@example.com",
            recipient_name="X",
            sender_name="Y",
            message="",
            stripe_payment_intent_id=pi_id,
        )
        pi = SimpleNamespace(
            id=pi_id,
            amount=1000,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "x@example.com",
                "recipient_name": "X",
                "sender_name": "Y",
                "is_scheduled": "False",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        assert GiftCard.objects.filter(stripe_payment_intent_id=pi_id).count() == 1
        mock_send_email.assert_not_called()

    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_invoice_payment_skipped_200(self, mock_construct, api_client):
        pi = SimpleNamespace(
            id="pi_invoice_skip",
            amount=1000,
            metadata={"booking_type": "Single Session"},
            invoice="in_123",
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK

    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_payment_failed_marks_pending_booking_cancelled(
        self, mock_construct, api_client
    ):
        business = BusinessFactory()
        cls = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=cls)
        schedule = ScheduleFactory(option=option)
        inst = ScheduleInstanceFactory(schedule=schedule)
        contact = ContactFactory(business=business)
        booking = BookingFactory(
            schedule_instance=inst,
            contact=contact,
            status="pending",
            payment_status="pending",
        )
        pi_id = "pi_fail_test_001"
        Payment.objects.create(
            booking=booking,
            stripe_payment_intent_id=pi_id,
            amount=Decimal("25.00"),
            status="pending",
        )
        err = SimpleNamespace(message="Card declined")
        pi = SimpleNamespace(
            id=pi_id,
            last_payment_error=err,
        )
        mock_construct.return_value = _evt_payment_intent_failed(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        booking.refresh_from_db()
        assert booking.status == "cancelled"
        assert booking.payment_status == "failed"

    @patch("quickstart.payments.views.send_purchase_event_for_booking")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_idempotency_already_succeeded_payment(
        self, mock_construct, mock_capi, api_client
    ):
        booking = BookingFactory(status="confirmed", payment_status="paid")
        PaymentFactory(
            booking=booking,
            stripe_payment_intent_id="pi_already_done",
            status="succeeded",
        )
        pi = SimpleNamespace(
            id="pi_already_done",
            amount_received=2500,
            metadata={
                "tax_amount": "0.00",
                "subtotal_for_payout": "25.00",
                "booking_type": "Single Session",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        body = r.json() if r.content else {}
        assert body.get("message") == "Already processed"
        mock_capi.assert_not_called()
