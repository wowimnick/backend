"""Tests for corporate shortlist public API, balance payment, and support."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework import status

from quickstart.models import (
    CorporateBooking,
    CorporateBookingEvent,
    CorporateInquiry,
    CorporateShortlist,
    CorporateShortlistOption,
    default_corporate_shortlist_presentation,
)
from quickstart.payments.corporate_stripe_webhooks import (
    handle_corporate_balance_pi_succeeded,
)
from quickstart.tests.factories import RoleFactory, UserFactory

API = "/api"


def _make_shortlist(*, shortlist_status=CorporateShortlist.STATUS_SENT, **presentation):
    inquiry = CorporateInquiry.objects.create(
        company_name="Acme Corp",
        contact_name="Jane Doe",
        email="jane@acme.example",
        phone="",
        company_size="11-25",
        message="Team event",
        meta={},
    )
    pres = default_corporate_shortlist_presentation()
    pres.update(presentation)
    sl = CorporateShortlist.objects.create(
        inquiry=inquiry,
        status=shortlist_status,
        intro_message="Welcome to your custom proposal.",
        presentation=pres,
        deposit_percent=25,
        sent_at=timezone.now(),
    )
    opt = CorporateShortlistOption.objects.create(
        shortlist=sl,
        position=1,
        title="Cooking Class",
        host_name="Chef Ana",
        price_total_cents=100_000,
        proposed_date_options=[timezone.now().isoformat()],
    )
    return sl, opt


def _make_booking(sl, opt, *, booking_status=CorporateBooking.ST_DEPOSIT_PAID):
    return CorporateBooking.objects.create(
        shortlist=sl,
        selected_option=opt,
        status=booking_status,
        headcount=20,
        confirmed_datetime=timezone.now() + timedelta(days=30),
        billing_company_name="Acme Corp",
        billing_contact_name="Jane Doe",
        billing_email="jane@acme.example",
        billing_address={
            "line1": "123 Main St",
            "line2": "Suite 4",
            "city": "New York",
            "state": "NY",
            "postal_code": "10001",
            "country": "US",
        },
        total_cents=100_000,
        deposit_cents=25_000,
        balance_cents=75_000,
        currency="usd",
    )


@pytest.mark.django_db
class TestCorporateShortlistPresentation:
    def test_public_shortlist_includes_intro_and_presentation(self, api_client):
        sl, _ = _make_shortlist(accent_color="#2563eb", cta_label="Pick one")
        url = f"{API}/corporate/shortlist/{sl.token}/"
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["intro_message"] == "Welcome to your custom proposal."
        assert data["presentation"]["accent_color"] == "#2563eb"
        assert data["presentation"]["cta_label"] == "Pick one"
        assert data["presentation"]["show_sections"]["show_comparison"] is True


@pytest.mark.django_db
class TestCorporateBalanceIntent:
    @patch("quickstart.services.corporate_billing.stripe.PaymentIntent.create")
    @patch("quickstart.services.corporate_billing.stripe.Customer.create")
    def test_balance_intent_created_when_deposit_paid(
        self, mock_customer_create, mock_pi_create, api_client
    ):
        mock_customer_create.return_value = MagicMock(id="cus_test")
        mock_pi_create.return_value = MagicMock(
            id="pi_balance",
            client_secret="pi_balance_secret",
        )
        sl, opt = _make_shortlist()
        booking = _make_booking(sl, opt)

        url = (
            f"{API}/corporate/shortlist/{sl.token}/booking/{booking.id}/balance-intent/"
        )
        response = api_client.post(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["client_secret"] == "pi_balance_secret"
        assert data["payment_intent_id"] == "pi_balance"
        mock_pi_create.assert_called_once()
        call_kwargs = mock_pi_create.call_args.kwargs
        assert call_kwargs["amount"] == 75_000
        assert call_kwargs["metadata"]["type"] == "corporate_balance_pi"
        assert call_kwargs["metadata"]["corporate_booking_id"] == str(booking.id)

    def test_balance_intent_rejected_when_pending_deposit(self, api_client):
        sl, opt = _make_shortlist()
        booking = _make_booking(sl, opt, booking_status=CorporateBooking.ST_PENDING)
        url = (
            f"{API}/corporate/shortlist/{sl.token}/booking/{booking.id}/balance-intent/"
        )
        response = api_client.post(url)
        assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.django_db
class TestCorporateBalancePiWebhook:
    @patch("quickstart.tasks.corporate_booking_tasks.send_balance_paid.delay")
    @patch("stripe.Invoice.void_invoice")
    def test_balance_pi_marks_fully_paid_and_voids_invoice(
        self, mock_void, mock_email_delay
    ):
        sl, opt = _make_shortlist()
        booking = _make_booking(
            sl,
            opt,
            booking_status=CorporateBooking.ST_INVOICED,
        )
        booking.stripe_invoice_id = "in_test123"
        booking.invoice_status = "open"
        booking.save(update_fields=["stripe_invoice_id", "invoice_status", "updated_at"])

        pi = {"id": "pi_bal_1", "metadata": {"type": "corporate_balance_pi", "corporate_booking_id": str(booking.id)}}
        response = handle_corporate_balance_pi_succeeded(pi, "evt_bal_1", "wh_test")

        assert response.status_code == status.HTTP_200_OK
        booking.refresh_from_db()
        assert booking.status == CorporateBooking.ST_FULLY_PAID
        assert booking.balance_paid_at is not None
        assert booking.invoice_status == "paid"
        mock_void.assert_called_once_with("in_test123")
        mock_email_delay.assert_called_once_with(str(booking.id))
        assert CorporateBookingEvent.objects.filter(
            booking=booking, event_type="balance_paid"
        ).exists()


@pytest.mark.django_db
class TestCorporateShortlistSupport:
    @patch("quickstart.tasks.corporate_booking_tasks.send_corporate_support_message.delay")
    def test_support_endpoint_queues_email_task(self, mock_delay, api_client):
        sl, _ = _make_shortlist()
        payload = {
            "name": "Jane Doe",
            "email": "jane@acme.example",
            "message": "Can we change the date?",
        }
        url = f"{API}/corporate/shortlist/{sl.token}/support/"
        response = api_client.post(url, data=payload, format="json")
        assert response.status_code == status.HTTP_202_ACCEPTED
        mock_delay.assert_called_once_with(
            str(sl.id),
            "Jane Doe",
            "jane@acme.example",
            "Can we change the date?",
        )

    def test_support_invalid_payload_returns_400(self, api_client):
        sl, _ = _make_shortlist()
        url = f"{API}/corporate/shortlist/{sl.token}/support/"
        response = api_client.post(url, data={"email": "bad"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestSendCorporateSupportMessageRecipients:
    def test_internal_and_customer_emails_sent(self):
        super_role = RoleFactory(name="Super Admin")
        UserFactory(email="corp-support-admin@example.com", role=super_role)
        sl, _ = _make_shortlist()

        with patch(
            "quickstart.tasks.corporate_booking_tasks._send_html"
        ) as mock_send:
            from quickstart.tasks.corporate_booking_tasks import (
                send_corporate_support_message,
            )

            send_corporate_support_message(
                str(sl.id),
                "Jane Doe",
                "jane@acme.example",
                "Need help with billing.",
            )

        assert mock_send.call_count == 2
        internal_call = mock_send.call_args_list[0]
        ack_call = mock_send.call_args_list[1]
        assert "corp-support-admin@example.com" in internal_call[0][0]
        assert internal_call[0][2] == "emails/corporate_support_message.html"
        assert ack_call[0][0] == ["jane@acme.example"]
        assert ack_call[0][2] == "emails/corporate_support_ack.html"
