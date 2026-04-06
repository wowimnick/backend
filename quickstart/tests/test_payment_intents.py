"""Tests for payment intent helpers: slot check, cancel pending, update intent validation."""
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.models import Booking, Payment
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ContactFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"


@pytest.mark.django_db
class TestCheckSlotAvailability:
    def test_missing_instance_id_400(self, api_client):
        r = api_client.get(f"{API}/payments/check-slot-availability/")
        assert r.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_instance_id_returns_unavailable(self, api_client):
        r = api_client.get(
            f"{API}/payments/check-slot-availability/",
            {"instance_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert r.status_code == status.HTTP_200_OK
        assert r.json().get("available") is False

    def test_valid_instance_returns_spots(self, api_client, schedule_instance):
        r = api_client.get(
            f"{API}/payments/check-slot-availability/",
            {"instance_id": str(schedule_instance.id), "participants": "1"},
        )
        assert r.status_code == status.HTTP_200_OK
        data = r.json()
        assert "available" in data
        assert data.get("available_spots", 0) >= 1


@pytest.mark.django_db
class TestCancelPendingBooking:
    @patch("quickstart.payments.views.stripe.PaymentIntent.cancel")
    def test_cancel_deletes_pending_booking_and_payment(self, mock_cancel, api_client):
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
        pi_id = "pi_cancel_flow_test"
        pay = Payment.objects.create(
            booking=booking,
            stripe_payment_intent_id=pi_id,
            amount=Decimal("25.00"),
            status="pending",
        )
        r = api_client.post(
            f"{API}/payments/cancel-payment-intent/",
            {"payment_intent_id": pi_id},
            format="json",
        )
        assert r.status_code == status.HTTP_200_OK
        mock_cancel.assert_called_once_with(pi_id)
        assert not Payment.objects.filter(pk=pay.pk).exists()
        assert not Booking.objects.filter(pk=booking.pk).exists()

    def test_missing_payment_intent_id_400(self, api_client):
        r = api_client.post(
            f"{API}/payments/cancel-payment-intent/",
            {},
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestUpdatePaymentIntentValidation:
    def test_missing_payment_intent_id_400(self, api_client):
        r = api_client.post(
            f"{API}/payments/update-payment-intent/",
            {"guest_email": "a@b.com"},
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST
