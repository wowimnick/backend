"""
Ensures merging Stripe transfer metadata into payout_record.metadata cannot raise
(the production bug was KeyError from treating StripeObject like a plain dict).

Run: pytest quickstart/tests/test_payout_metadata_merge.py -v
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from quickstart.models import Payment, Payout
from quickstart.tasks.payout_tasks import _estimate_stripe_processing_fee
from quickstart.tests.factories import (
    BusinessFactory,
    BookingFactory,
    ScheduleInstanceFactory,
)
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict


def test_merge_metadata_like_payout_task():
    base = {"source": "daily_payout_task", "temp_id": True}
    transfer = SimpleNamespace(
        metadata=SimpleNamespace(
            to_dict=lambda: {"business_id": "99", "payout_record_id": "abc"}
        )
    )
    updated = dict(base)
    updated.update(stripe_metadata_to_dict(getattr(transfer, "metadata", None)))
    updated.pop("temp_id", None)
    assert updated["business_id"] == "99"
    assert updated["payout_record_id"] == "abc"
    assert "temp_id" not in updated


@pytest.mark.django_db
def test_process_daily_payouts_completes_with_stripe_like_metadata():
    """Single business, one booking: task runs through save() after Transfer.create."""
    business = BusinessFactory(
        stripe_account_id="acct_test_meta_merge",
        currency="CAD",
    )
    yesterday = timezone.now().date() - timedelta(days=1)

    inst = ScheduleInstanceFactory(
        schedule__option__classId__businessId=business,
        date=yesterday,
    )
    booking = BookingFactory(
        schedule_instance=inst,
        status="completed",
        payment_status="paid",
        payout_status="pending",
        amount_paid=Decimal("100.00"),
        allocated_net_payout=Decimal("100.00"),
    )
    Payment.objects.create(
        booking=booking,
        stripe_payment_intent_id="pi_test_meta_merge_unique",
        amount=Decimal("100.00"),
        status="succeeded",
    )

    fee = _estimate_stripe_processing_fee(Decimal("100.00"))
    expected_net = (Decimal("100.00") - fee).quantize(Decimal("0.01"))
    expected_cents = int(expected_net * 100)

    class MetaObj:
        def to_dict(self):
            return {"business_id": str(business.businessId), "booking_count": "1"}

    transfer = MagicMock()
    transfer.id = "tr_test_meta_merge_001"
    transfer.currency = "cad"
    transfer.metadata = MetaObj()

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create", return_value=transfer):
            from quickstart.tasks.payout_tasks import process_daily_payouts

            summary = process_daily_payouts()

    assert "Failed: 0" in summary
    booking.refresh_from_db()
    assert booking.payout_status == "processed"
    payout = Payout.objects.get(stripe_transfer_id="tr_test_meta_merge_001")
    assert str(payout.metadata.get("business_id")) == str(business.businessId)
    assert payout.amount == expected_net

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create") as mock_create:
            summary2 = process_daily_payouts()
    assert summary2 == "No bookings to pay out."
    mock_create.assert_not_called()
