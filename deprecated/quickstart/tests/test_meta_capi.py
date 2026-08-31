"""
Tests for Meta CAPI utils: fbc/fbp from cookies or request body (Parameter Builder best practice).
Run with: pytest quickstart/tests/test_meta_capi.py -v
"""
from unittest.mock import MagicMock, patch

import pytest

from quickstart.utils.meta_capi import _user_data_from_booking, send_purchase_event_for_booking
from quickstart.tests.factories import (
    BookingFactory,
    UserFactory,
    ContactFactory,
)


@pytest.mark.django_db
class TestMetaCapiUserDataFbcFbp:
    """Test that fbc and fbp are included in user_data when present in request."""

    def test_user_data_includes_fbc_fbp_from_cookies(self):
        """When request has _fbc and _fbp cookies, user_data contains fbc and fbp (pass-through)."""
        user = UserFactory(email="user@example.com", first_name="Jane", last_name="Doe")
        booking = BookingFactory(user=user, contact=None)
        request = MagicMock()
        request.COOKIES = {"_fbc": "fb.1.123.Ii4xMjM0", "_fbp": "fb.1.456.789"}
        request.META = {}
        request.data = {}

        user_data = _user_data_from_booking(booking, request=request)

        assert user_data is not None
        assert user_data.get("fbc") == "fb.1.123.Ii4xMjM0"
        assert user_data.get("fbp") == "fb.1.456.789"

    def test_user_data_includes_fbc_fbp_from_body_when_no_cookies(self):
        """When request has no cookies but data has meta_fbc/meta_fbp, user_data includes them."""
        contact = ContactFactory(
            email="guest@example.com",
            first_name="Guest",
            last_name="User",
        )
        booking = BookingFactory(user=None, contact=contact)
        request = MagicMock()
        request.COOKIES = {}
        request.META = {}
        request.data = {"meta_fbc": "fb.1.body.Ii4x", "meta_fbp": "fb.1.body.789"}

        user_data = _user_data_from_booking(booking, request=request)

        assert user_data is not None
        assert user_data.get("fbc") == "fb.1.body.Ii4x"
        assert user_data.get("fbp") == "fb.1.body.789"

    def test_cookies_take_precedence_over_body(self):
        """Cookies are preferred over request.data for fbc/fbp."""
        user = UserFactory(email="a@b.com", first_name="A", last_name="B")
        booking = BookingFactory(user=user, contact=None)
        request = MagicMock()
        request.COOKIES = {"_fbc": "from_cookie", "_fbp": "from_cookie"}
        request.META = {}
        request.data = {"meta_fbc": "from_body", "meta_fbp": "from_body"}

        user_data = _user_data_from_booking(booking, request=request)

        assert user_data.get("fbc") == "from_cookie"
        assert user_data.get("fbp") == "from_cookie"

    def test_no_fbc_fbp_when_request_none(self):
        """When request is None, user_data has no fbc/fbp."""
        user = UserFactory(email="x@y.com", first_name="X", last_name="Y")
        booking = BookingFactory(user=user, contact=None)

        user_data = _user_data_from_booking(booking, request=None)

        assert user_data is not None
        assert "fbc" not in user_data
        assert "fbp" not in user_data


@pytest.mark.django_db
class TestSendPurchaseEventForBookingPaidConversion:
    """Test that paid conversions (webhook) get fbc/fbp from meta_fbc/meta_fbp kwargs."""

    @patch("quickstart.utils.meta_capi.send_purchase_event")
    def test_meta_fbc_fbp_merged_into_user_data_when_request_none(
        self, mock_send_purchase_event
    ):
        """When request is None (webhook) but meta_fbc/meta_fbp are passed, CAPI receives them."""
        mock_send_purchase_event.return_value = True
        user = UserFactory(email="paid@example.com", first_name="Paid", last_name="User")
        booking = BookingFactory(user=user, contact=None)

        send_purchase_event_for_booking(
            booking,
            value=99.00,
            currency="CAD",
            num_items=1,
            request=None,
            meta_fbc="fb.1.webhook.Ii4x",
            meta_fbp="fb.1.webhook.789",
        )

        mock_send_purchase_event.assert_called_once()
        call_kwargs = mock_send_purchase_event.call_args[1]
        user_data = call_kwargs.get("user_data") or {}
        assert user_data.get("fbc") == "fb.1.webhook.Ii4x"
        assert user_data.get("fbp") == "fb.1.webhook.789"
