"""
Tests for ProcessBookingWebhook (Stripe booking / gift-card payments).
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.models import Booking, GiftCard, Payment
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ContactFactory,
    PartnerTierFactory,
    PaymentFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"
HST = Decimal("0.13")


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

    @patch("quickstart.payments.views.send_super_admin_booking_webhook_failed_email")
    @patch("quickstart.payments.views.stripe.Refund.create")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_validation_failure_notifies_admins_without_refund(
        self, mock_construct, mock_refund_create, mock_notify, api_client
    ):
        pi_id = "pi_validation_no_refund"
        pi = SimpleNamespace(
            id=pi_id,
            amount_received=1000,
            latest_charge="ch_test",
            metadata={
                "booking_type": "Single Session",
                "participants": "1",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        body = r.json()
        assert body.get("manual_review") is True
        mock_refund_create.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["payment_intent_id"] == pi_id

    @patch("quickstart.payments.views.send_purchase_event_for_booking")
    @patch("quickstart.payments.views.send_super_admin_booking_created_email")
    @patch("quickstart.payments.views.send_business_new_booking_email")
    @patch("quickstart.payments.views.send_booking_confirmation_email")
    @patch("quickstart.payments.views.send_sms_task")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_placeholder_contact_synced_from_metadata_creates_booking(
        self,
        mock_construct,
        _sms,
        _confirm,
        _biz,
        _super,
        _capi,
        api_client,
    ):
        PartnerTierFactory(is_default=True, name="default-tier-placeholder-sync")
        business = BusinessFactory(isActive=True, verificationStatus="verified")
        cls = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=cls, booking_type="Single Session")
        schedule = ScheduleFactory(option=option)
        inst = ScheduleInstanceFactory(schedule=schedule, status="scheduled")
        contact = ContactFactory(
            business=business,
            email="pending@example.com",
            phone_number="555-555-5555",
        )
        subtotal = Decimal(inst.price)
        tax = (subtotal * HST).quantize(Decimal("0.01"))
        grand_cents = int((subtotal + tax) * 100)

        pi_id = "pi_placeholder_sync_test"
        pi = SimpleNamespace(
            id=pi_id,
            amount_received=grand_cents,
            latest_charge="ch_placeholder_sync",
            metadata={
                "schedule_instance_id": str(inst.id),
                "participants": "1",
                "booking_type": "Single Session",
                "is_guest": "True",
                "guest_contact_id": str(contact.id),
                "guest_email": "nina@example.com",
                "guest_full_name": "Nina DeGagne",
                "guest_phone": "8609449143",
                "participant_details_json": '[{"name": "Nina DeGagne"}]',
                "notes": "Birthday gift",
                "tax_amount": str(tax),
                "subtotal_for_payout": str(subtotal),
                "subtotal_after_discount": str(subtotal),
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = self._post_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK

        payment = Payment.objects.get(stripe_payment_intent_id=pi_id)
        booking = payment.booking
        assert booking.status == "confirmed"
        assert booking.payment_status == "paid"

        contact.refresh_from_db()
        assert contact.email == "nina@example.com"
        assert contact.phone_number == "8609449143"

        assert Booking.objects.filter(pk=booking.pk).exists()
