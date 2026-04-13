"""
Gift card lifecycle tests: models, public endpoints, checkout redemption,
webhooks, refunds, scheduled delivery, and first-purchase promo.

Run: pytest quickstart/tests/test_gift_cards.py -v
"""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework import status

from quickstart.models import (
    FirstPurchaseGiftCardSent,
    GiftCard,
    GiftCardTransaction,
    Payment,
)
from quickstart.payments.views import try_send_first_purchase_gift_card
from quickstart.tasks.giftcard_tasks import process_scheduled_gift_cards
from quickstart.tasks.payout_tasks import process_daily_refunds
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ContactFactory,
    GiftCardFactory,
    PaymentFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"
HST = Decimal("0.13")


def _grand_total_for_instance(schedule_instance, participants=1):
    """Subtotal * participants + HST (matches CreatePaymentIntentView)."""
    subtotal = Decimal(schedule_instance.price) * participants
    tax = (subtotal * HST).quantize(Decimal("0.01"))
    return subtotal + tax


def _guest_intent_payload(schedule_instance, gift_card_code=None, **extra):
    payload = {
        "guest_email": "buyer@example.com",
        "guest_full_name": "Buyer Test",
        "guest_phone": "+15551234567",
        "selectedSlots": [{"id": schedule_instance.id, "isCourse": False}],
        "participants": 1,
    }
    if gift_card_code is not None:
        payload["gift_card_code"] = gift_card_code
    payload.update(extra)
    return payload


def _evt_payment_intent_succeeded(pi_obj):
    return SimpleNamespace(
        type="payment_intent.succeeded",
        data=SimpleNamespace(object=pi_obj),
        id="evt_test_pi_succeeded_gc",
    )


def _post_booking_webhook(api_client):
    return api_client.post(
        f"{API}/payments/webhook/",
        data=b"{}",
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE="sig_test",
    )


# -----------------------------------------------------------------------------
# Models & ledger
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardModels:
    def test_gift_card_code_auto_generated(self):
        gc = GiftCard(
            initial_amount=Decimal("10.00"),
            current_balance=Decimal("10.00"),
            recipient_email="r@example.com",
            recipient_name="R",
            sender_name="S",
            message="",
        )
        gc.save()
        assert gc.code
        assert len(gc.code.replace("-", "")) >= 12

    def test_gift_card_initial_load_transaction(self):
        gc = GiftCardFactory(current_balance=Decimal("25.00"), initial_amount=Decimal("25.00"))
        GiftCardTransaction.objects.create(
            gift_card=gc,
            amount=Decimal("25.00"),
            balance_after=Decimal("25.00"),
            transaction_type="initial_load",
        )
        assert gc.transactions.count() == 1
        assert gc.transactions.first().transaction_type == "initial_load"

    def test_gift_card_redemption_updates_balance(self):
        gc = GiftCardFactory(current_balance=Decimal("50.00"), initial_amount=Decimal("50.00"))
        booking = BookingFactory()
        gc.current_balance -= Decimal("15.00")
        gc.save()
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=booking,
            amount=Decimal("-15.00"),
            balance_after=gc.current_balance,
            transaction_type="redemption",
        )
        gc.refresh_from_db()
        assert gc.current_balance == Decimal("35.00")
        assert GiftCardTransaction.objects.filter(transaction_type="redemption").count() == 1

    def test_gift_card_refund_restores_balance(self):
        gc = GiftCardFactory(current_balance=Decimal("40.00"), initial_amount=Decimal("50.00"))
        booking = BookingFactory()
        gc.current_balance += Decimal("10.00")
        gc.save()
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=booking,
            amount=Decimal("10.00"),
            balance_after=gc.current_balance,
            transaction_type="refund",
        )
        gc.refresh_from_db()
        assert gc.current_balance == Decimal("50.00")

    def test_gift_card_transaction_ledger_consistency(self):
        gc = GiftCardFactory(current_balance=Decimal("0.00"), initial_amount=Decimal("100.00"))
        b1 = BookingFactory()
        b2 = BookingFactory()
        GiftCardTransaction.objects.create(
            gift_card=gc,
            amount=Decimal("100.00"),
            balance_after=Decimal("100.00"),
            transaction_type="initial_load",
        )
        gc.current_balance = Decimal("100.00")
        gc.save()
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=b1,
            amount=Decimal("-30.00"),
            balance_after=Decimal("70.00"),
            transaction_type="redemption",
        )
        gc.current_balance = Decimal("70.00")
        gc.save()
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=b2,
            amount=Decimal("10.00"),
            balance_after=Decimal("80.00"),
            transaction_type="refund",
        )
        gc.current_balance = Decimal("80.00")
        gc.save()
        txns = list(gc.transactions.order_by("created_at"))
        assert [t.balance_after for t in txns] == [
            Decimal("100.00"),
            Decimal("70.00"),
            Decimal("80.00"),
        ]


# -----------------------------------------------------------------------------
# Validate endpoint
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardValidateEndpoint:
    def test_validate_inactive_card_404(self, api_client):
        gc = GiftCardFactory(is_active=False, current_balance=Decimal("50.00"))
        r = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": gc.code},
            format="json",
        )
        assert r.status_code == status.HTTP_404_NOT_FOUND

    def test_validate_case_insensitive_code(self, api_client):
        gc = GiftCardFactory(code="ABCD-1234-EFGH-5678", current_balance=Decimal("10.00"))
        r = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": "abcd-1234-efgh-5678"},
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        data = r.json()
        assert data["code"] == gc.code
        assert Decimal(str(data["balance"])) == Decimal("10.00")

    def test_validate_returns_code_and_balance(self, api_client, gift_card):
        r = api_client.post(
            f"{API}/gift-cards/validate/",
            {"code": gift_card.code},
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        body = r.json()
        assert "code" in body and "balance" in body


# -----------------------------------------------------------------------------
# Purchase intent endpoint
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardPurchaseIntentEndpoint:
    def test_purchase_intent_minimum_amount_400(self, api_client):
        r = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {
                "amount": "4.00",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
            },
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST

    @patch("quickstart.views.public.public_giftcard_views.stripe.PaymentIntent.create")
    def test_purchase_intent_with_schedule_date(self, mock_create, api_client):
        mock_create.return_value = MagicMock(client_secret="pi_sec")
        scheduled = (date.today() + timedelta(days=30)).isoformat()
        r = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {
                "amount": "50.00",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "date": scheduled,
            },
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        call_kw = mock_create.call_args[1]
        meta = call_kw["metadata"]
        assert meta.get("is_scheduled") == "True"
        assert scheduled in (meta.get("scheduled_date") or "")

    @patch("quickstart.views.public.public_giftcard_views.stripe.PaymentIntent.create")
    def test_purchase_intent_send_to_self(self, mock_create, api_client):
        mock_create.return_value = MagicMock(client_secret="pi_sec")
        r = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {
                "amount": "50.00",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "send_to_self": True,
            },
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        meta = mock_create.call_args[1]["metadata"]
        assert meta.get("send_to_self") == "True"

    @patch("quickstart.views.public.public_giftcard_views.stripe.PaymentIntent.create")
    def test_purchase_intent_stripe_error_500(self, mock_create, api_client):
        mock_create.side_effect = Exception("stripe down")
        r = api_client.post(
            f"{API}/gift-cards/purchase-intent/",
            {
                "amount": "50.00",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
            },
            format="json",
        )
        assert r.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "error" in r.json()


# -----------------------------------------------------------------------------
# Webhook: gift card purchase creation
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardWebhookCreation:
    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_webhook_creates_initial_load_transaction(
        self, mock_construct, mock_send, api_client
    ):
        pi_id = "pi_gc_txn_check"
        pi = SimpleNamespace(
            id=pi_id,
            amount=7500,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "message": "",
                "is_scheduled": "False",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = _post_booking_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        gc = GiftCard.objects.get(stripe_payment_intent_id=pi_id)
        txn = GiftCardTransaction.objects.filter(
            gift_card=gc, transaction_type="initial_load"
        ).first()
        assert txn is not None
        assert txn.amount == Decimal("75.00")
        assert txn.balance_after == Decimal("75.00")

    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_webhook_gift_card_design_url_metadata(
        self, mock_construct, mock_send, api_client
    ):
        pi_id = "pi_gc_design"
        pi = SimpleNamespace(
            id=pi_id,
            amount=5000,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "is_scheduled": "False",
                "design_url": "https://cdn.example.com/card.png",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = _post_booking_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        gc = GiftCard.objects.get(stripe_payment_intent_id=pi_id)
        assert gc.design_url == "https://cdn.example.com/card.png"

    @patch("quickstart.payments.views.send_gift_card_email")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_webhook_gift_card_send_to_self_metadata(
        self, mock_construct, mock_send, api_client
    ):
        pi_id = "pi_gc_self"
        pi = SimpleNamespace(
            id=pi_id,
            amount=3000,
            metadata={
                "type": "gift_card_purchase",
                "recipient_email": "r@example.com",
                "recipient_name": "R",
                "sender_name": "S",
                "is_scheduled": "False",
                "send_to_self": "true",
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = _post_booking_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK
        gc = GiftCard.objects.get(stripe_payment_intent_id=pi_id)
        assert gc.send_to_self is True


# -----------------------------------------------------------------------------
# Checkout redemption (CreatePaymentIntentView)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardCheckoutRedemption:
    @patch("quickstart.payments.views.trigger_multiple_revalidations")
    @patch("quickstart.payments.views.trigger_nextjs_revalidation")
    @patch("quickstart.payments.views.send_purchase_event_for_booking")
    @patch("quickstart.payments.views.send_super_admin_booking_created_email")
    @patch("quickstart.payments.views.send_business_new_booking_email")
    @patch("quickstart.payments.views.send_booking_confirmation_email")
    @patch("quickstart.payments.views.send_sms_task")
    @patch("quickstart.payments.views.stripe.PaymentIntent.create")
    def test_checkout_gift_card_full_cover_no_stripe(
        self,
        mock_pi_create,
        _sms,
        _confirm,
        _biz,
        _super,
        _capi,
        _next_rev,
        _multi_rev,
        api_client,
        schedule_instance,
        gift_card_with_balance,
    ):
        grand = _grand_total_for_instance(schedule_instance)
        assert gift_card_with_balance.current_balance >= grand
        balance_before = gift_card_with_balance.current_balance

        payload = _guest_intent_payload(
            schedule_instance, gift_card_code=gift_card_with_balance.code
        )
        r = api_client.post(
            f"{API}/payments/create-payment-intent/",
            payload,
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        mock_pi_create.assert_not_called()
        data = r.json()
        assert data.get("status") == "confirmed"
        assert "booking_id" in data

        gift_card_with_balance.refresh_from_db()
        assert gift_card_with_balance.current_balance == balance_before - grand

        booking_id = data["booking_id"]
        assert GiftCardTransaction.objects.filter(
            booking_id=booking_id,
            transaction_type="redemption",
            gift_card=gift_card_with_balance,
        ).exists()

    @patch("quickstart.payments.views.stripe.PaymentIntent.create")
    def test_checkout_gift_card_partial_cover_stripe_metadata(
        self, mock_pi_create, api_client, schedule_instance
    ):
        grand = _grand_total_for_instance(schedule_instance)
        gc = GiftCardFactory(current_balance=Decimal("10.00"), initial_amount=Decimal("10.00"))
        mock_pi_create.return_value = MagicMock(id="pi_partial", client_secret="sec")

        r = api_client.post(
            f"{API}/payments/create-payment-intent/",
            _guest_intent_payload(schedule_instance, gift_card_code=gc.code),
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        mock_pi_create.assert_called_once()
        meta = mock_pi_create.call_args[1]["metadata"]
        assert meta.get("gift_card_code") == gc.code
        assert Decimal(meta.get("gift_card_amount_to_deduct")) == Decimal("10.00")
        remainder = grand - Decimal("10.00")
        expected_cents = int((remainder * 100).to_integral_value())
        assert mock_pi_create.call_args[1]["amount"] == expected_cents

        gc.refresh_from_db()
        assert gc.current_balance == Decimal("10.00")

    def test_checkout_invalid_gift_card_code_400(self, api_client, schedule_instance):
        r = api_client.post(
            f"{API}/payments/create-payment-intent/",
            _guest_intent_payload(schedule_instance, gift_card_code="NOPE-NOPE-NOPE-NOPE"),
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST
        assert "Invalid Gift Card Code" in str(r.json())

    @patch("quickstart.payments.views.stripe.PaymentIntent.create")
    def test_checkout_zero_balance_gift_card_full_stripe_charge(
        self, mock_pi_create, api_client, schedule_instance, gift_card_zero_balance
    ):
        grand = _grand_total_for_instance(schedule_instance)
        mock_pi_create.return_value = MagicMock(id="pi_full", client_secret="sec")
        r = api_client.post(
            f"{API}/payments/create-payment-intent/",
            _guest_intent_payload(
                schedule_instance, gift_card_code=gift_card_zero_balance.code
            ),
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        mock_pi_create.assert_called_once()
        assert mock_pi_create.call_args[1]["amount"] == int(grand * 100)

    def test_checkout_below_stripe_minimum_after_gc_400(
        self, api_client, schedule_instance
    ):
        grand = _grand_total_for_instance(schedule_instance)
        cover = grand - Decimal("0.44")
        gc = GiftCardFactory(current_balance=cover, initial_amount=cover)
        r = api_client.post(
            f"{API}/payments/create-payment-intent/",
            _guest_intent_payload(schedule_instance, gift_card_code=gc.code),
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST
        assert "too low" in str(r.json()).lower()

    @patch("quickstart.payments.views.trigger_multiple_revalidations")
    @patch("quickstart.payments.views.trigger_nextjs_revalidation")
    @patch("quickstart.payments.views.send_purchase_event_for_booking")
    @patch("quickstart.payments.views.send_super_admin_booking_created_email")
    @patch("quickstart.payments.views.send_business_new_booking_email")
    @patch("quickstart.payments.views.send_booking_confirmation_email")
    @patch("quickstart.payments.views.send_sms_task")
    @patch("quickstart.payments.views.stripe.Webhook.construct_event")
    def test_webhook_deducts_gift_card_single_session_instant_flow(
        self,
        mock_construct,
        _sms,
        _confirm,
        _biz,
        _super,
        _capi,
        _next_rev,
        _multi_rev,
        api_client,
    ):
        business = BusinessFactory(isActive=True, verificationStatus="verified")
        cls = ClassMainFactory(businessId=business, status="active")
        option = ClassOptionFactory(classId=cls, booking_type="Single Session")
        schedule = ScheduleFactory(option=option)
        inst = ScheduleInstanceFactory(schedule=schedule, status="scheduled")
        contact = ContactFactory(business=business)

        gc = GiftCardFactory(
            code="WEBHOOK-GC-TEST-0001",
            current_balance=Decimal("50.00"),
            initial_amount=Decimal("50.00"),
        )
        grand = _grand_total_for_instance(inst)
        gc_amount = grand - Decimal("5.00")
        stripe_part_cents = int((Decimal("5.00") * 100).to_integral_value())

        pi_id = "pi_booking_gc_deduct"
        pi = SimpleNamespace(
            id=pi_id,
            amount_received=stripe_part_cents,
            latest_charge="ch_test",
            metadata={
                "schedule_instance_id": str(inst.id),
                "participants": "1",
                "booking_type": "Single Session",
                "is_guest": "True",
                "guest_contact_id": str(contact.id),
                "guest_email": contact.email,
                "guest_full_name": "Test User",
                "guest_phone": "+15559876543",
                "participant_details_json": '[{"name": "Test User"}]',
                "notes": "",
                "tax_amount": str((Decimal(inst.price) * HST).quantize(Decimal("0.01"))),
                "subtotal_for_payout": str(Decimal(inst.price)),
                "subtotal_after_discount": str(Decimal(inst.price)),
                "gift_card_code": gc.code,
                "gift_card_amount_to_deduct": str(gc_amount),
            },
            invoice=None,
        )
        mock_construct.return_value = _evt_payment_intent_succeeded(pi)
        r = _post_booking_webhook(api_client)
        assert r.status_code == status.HTTP_200_OK

        gc.refresh_from_db()
        assert gc.current_balance == Decimal("50.00") - gc_amount

        booking = Payment.objects.get(stripe_payment_intent_id=pi_id).booking
        assert GiftCardTransaction.objects.filter(
            gift_card=gc,
            booking=booking,
            transaction_type="redemption",
        ).exists()


# -----------------------------------------------------------------------------
# Refunds (process_daily_refunds)
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestGiftCardRefunds:
    @patch("quickstart.tasks.payout_tasks.stripe.Refund.create")
    def test_refund_restores_gift_card_and_creates_refund_transaction(
        self, mock_refund, business, schedule_instance
    ):
        contact = ContactFactory(business=business)
        gc = GiftCardFactory(current_balance=Decimal("90.00"), initial_amount=Decimal("100.00"))
        booking = BookingFactory(
            schedule_instance=schedule_instance,
            contact=contact,
            amount_paid=Decimal("28.25"),
            payment_status="refund_pending",
            cancellation_refund_percentage=100,
        )
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=booking,
            amount=Decimal("-10.00"),
            balance_after=Decimal("90.00"),
            transaction_type="redemption",
        )
        PaymentFactory(
            booking=booking,
            stripe_payment_intent_id="pi_mixed_test",
            amount=Decimal("18.25"),
            status="succeeded",
            refunded_amount=Decimal("0.00"),
        )

        process_daily_refunds()

        mock_refund.assert_called()
        booking.refresh_from_db()
        assert booking.payment_status == "refunded"

        gc.refresh_from_db()
        assert gc.current_balance == Decimal("100.00")

        assert GiftCardTransaction.objects.filter(
            gift_card=gc,
            booking=booking,
            transaction_type="refund",
            amount=Decimal("10.00"),
        ).exists()

    @patch("quickstart.tasks.payout_tasks.stripe.Refund.create")
    def test_refund_partial_gc_partial_stripe(self, mock_refund, business, schedule_instance):
        contact = ContactFactory(business=business)
        gc = GiftCardFactory(current_balance=Decimal("85.00"), initial_amount=Decimal("100.00"))
        booking = BookingFactory(
            schedule_instance=schedule_instance,
            contact=contact,
            amount_paid=Decimal("50.00"),
            payment_status="refund_pending",
            cancellation_refund_percentage=100,
        )
        GiftCardTransaction.objects.create(
            gift_card=gc,
            booking=booking,
            amount=Decimal("-15.00"),
            balance_after=Decimal("85.00"),
            transaction_type="redemption",
        )
        PaymentFactory(
            booking=booking,
            stripe_payment_intent_id="pi_split_refund",
            amount=Decimal("35.00"),
            status="succeeded",
        )

        process_daily_refunds()

        gc.refresh_from_db()
        assert gc.current_balance == Decimal("100.00")
        assert mock_refund.called
        booking.refresh_from_db()
        assert booking.payment_status == "refunded"


# -----------------------------------------------------------------------------
# Scheduled delivery task
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestProcessScheduledGiftCards:
    @patch("quickstart.tasks.giftcard_tasks.send_gift_card_email")
    def test_process_scheduled_sends_due_cards(self, mock_send, scheduled_gift_card):
        msg = process_scheduled_gift_cards()
        assert "Successfully processed" in msg
        mock_send.assert_called_once()
        args_gc = mock_send.call_args[0][0]
        assert args_gc.pk == scheduled_gift_card.pk

    @patch("quickstart.tasks.giftcard_tasks.send_gift_card_email")
    def test_process_scheduled_skips_future_cards(self, mock_send):
        future = date.today() + timedelta(days=7)
        GiftCardFactory(
            is_scheduled=True,
            scheduled_date=future,
            email_sent=False,
            is_active=True,
        )
        process_scheduled_gift_cards()
        mock_send.assert_not_called()

    @patch("quickstart.tasks.giftcard_tasks.send_gift_card_email")
    def test_process_scheduled_skips_already_sent(self, mock_send, scheduled_gift_card):
        scheduled_gift_card.email_sent = True
        scheduled_gift_card.save(update_fields=["email_sent"])
        process_scheduled_gift_cards()
        mock_send.assert_not_called()

    @patch("quickstart.tasks.giftcard_tasks.send_gift_card_email")
    def test_process_scheduled_skips_inactive_cards(self, mock_send):
        past = date.today() - timedelta(days=1)
        GiftCardFactory(
            is_scheduled=True,
            scheduled_date=past,
            email_sent=False,
            is_active=False,
        )
        process_scheduled_gift_cards()
        mock_send.assert_not_called()

    def test_process_scheduled_returns_message_when_none_due(self):
        out = process_scheduled_gift_cards()
        assert "No scheduled" in out or "scheduled" in out.lower()


# -----------------------------------------------------------------------------
# First-purchase promotional gift card
# -----------------------------------------------------------------------------


@pytest.mark.django_db
class TestFirstPurchaseGiftCard:
    @patch("quickstart.payments.views.send_gift_card_email")
    def test_first_purchase_75_creates_10_card(self, mock_send):
        try_send_first_purchase_gift_card(
            {"guest_email": "new@example.com", "guest_full_name": "N User"},
            Decimal("80.00"),
            payment_intent_id="pi_fp1",
        )
        assert GiftCard.objects.filter(recipient_email="new@example.com").exists()
        gc = GiftCard.objects.get(recipient_email="new@example.com")
        assert gc.current_balance == Decimal("10.00")
        mock_send.assert_called_once()

    @patch("quickstart.payments.views.send_gift_card_email")
    def test_first_purchase_100_creates_15_card(self, mock_send):
        try_send_first_purchase_gift_card(
            {"guest_email": "tier2@example.com"},
            Decimal("150.00"),
        )
        gc = GiftCard.objects.get(recipient_email="tier2@example.com")
        assert gc.current_balance == Decimal("15.00")

    @patch("quickstart.payments.views.send_gift_card_email")
    def test_first_purchase_200_creates_20_card(self, mock_send):
        try_send_first_purchase_gift_card(
            {"guest_email": "tier3@example.com"},
            Decimal("250.00"),
        )
        gc = GiftCard.objects.get(recipient_email="tier3@example.com")
        assert gc.current_balance == Decimal("20.00")

    @patch("quickstart.payments.views.send_gift_card_email")
    def test_first_purchase_below_threshold_no_card(self, mock_send):
        try_send_first_purchase_gift_card(
            {"guest_email": "low@example.com"},
            Decimal("50.00"),
        )
        assert not GiftCard.objects.filter(recipient_email="low@example.com").exists()
        mock_send.assert_not_called()

    @patch("quickstart.payments.views.send_gift_card_email")
    def test_first_purchase_idempotent_second_call(self, mock_send):
        meta = {"guest_email": "once@example.com", "guest_full_name": "O"}
        try_send_first_purchase_gift_card(meta, Decimal("100.00"))
        try_send_first_purchase_gift_card(meta, Decimal("200.00"))
        assert GiftCard.objects.filter(recipient_email="once@example.com").count() == 1
        assert FirstPurchaseGiftCardSent.objects.filter(
            customer_email="once@example.com"
        ).count() == 1
