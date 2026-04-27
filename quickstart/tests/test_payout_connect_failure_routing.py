"""
Tests for routing Stripe transfer failures to business connect email vs super-admin.

Run: pytest quickstart/tests/test_payout_connect_failure_routing.py -v
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import stripe
from django.utils import timezone

from quickstart.tasks.payout_tasks import (
    _is_business_connect_not_ready_for_payout,
    _send_payout_connect_required_immediate,
    _stripe_transfer_destination_not_ready,
)


def _biz(stripe_account_status="active", bid=42):
    return SimpleNamespace(
        businessId=bid,
        stripe_account_status=stripe_account_status,
        owner=SimpleNamespace(email="owner@example.com"),
        currency="CAD",
        last_payout_connect_reminder_sent=None,
    )


def test_stripe_transfer_destination_not_ready_positive():
    err = stripe.StripeError("Transfers are not enabled for this connected account.")
    assert _stripe_transfer_destination_not_ready(err) is True


def test_stripe_transfer_destination_not_ready_balance_insufficient():
    err = stripe.StripeError("Balance insufficient")
    err.code = "balance_insufficient"
    assert _stripe_transfer_destination_not_ready(err) is False


def test_stripe_transfer_destination_not_ready_generic():
    err = stripe.StripeError("Internal API error")
    err.code = "api_error"
    assert _stripe_transfer_destination_not_ready(err) is False


def test_is_connect_not_ready_when_status_incomplete():
    business = _biz("incomplete")
    err = stripe.StripeError("Something went wrong")
    assert _is_business_connect_not_ready_for_payout(business, err) is True


def test_is_connect_not_ready_when_status_active_but_message_matches():
    business = _biz("active")
    err = stripe.StripeError(
        "Cannot create a transfer: charges are not enabled on this account."
    )
    assert _is_business_connect_not_ready_for_payout(business, err) is True


def test_is_connect_not_ready_false_when_active_and_generic_error():
    business = _biz("active")
    err = stripe.StripeError("Rate limit exceeded")
    err.code = "rate_limit"
    assert _is_business_connect_not_ready_for_payout(business, err) is False


def test_send_payout_connect_required_immediate_calls_email():
    business = _biz("restricted", bid=7)
    with patch(
        "quickstart.tasks.payout_tasks._pending_payout_totals_for_business",
        return_value=(Decimal("25.50"), 2),
    ):
        with patch(
            "quickstart.utils.email_utils.send_payout_connect_required_email"
        ) as mock_send:
            with patch("quickstart.tasks.payout_tasks.BusinessInfo.objects.filter") as m_q:
                m_q.return_value.update = MagicMock()
                _send_payout_connect_required_immediate(business)
    mock_send.assert_called_once()
    kwargs = mock_send.call_args[1]
    assert kwargs["pending_amount"] == Decimal("25.50")
    assert kwargs["booking_count"] == 2
    m_q.assert_called_once_with(pk=7)
    m_q.return_value.update.assert_called_once()


def test_send_payout_connect_required_immediate_respects_cooldown():
    business = _biz("restricted", bid=8)
    business.last_payout_connect_reminder_sent = timezone.now()
    with patch(
        "quickstart.tasks.payout_tasks._pending_payout_totals_for_business",
        return_value=(Decimal("10.00"), 1),
    ):
        with patch(
            "quickstart.utils.email_utils.send_payout_connect_required_email"
        ) as mock_send:
            with patch("quickstart.tasks.payout_tasks.BusinessInfo.objects.filter") as m_q:
                m_q.return_value.update = MagicMock()
                _send_payout_connect_required_immediate(business)

    mock_send.assert_not_called()
    m_q.assert_not_called()
