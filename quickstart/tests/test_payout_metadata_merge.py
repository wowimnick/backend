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
from quickstart.tests.factories import (
    BusinessFactory,
    BookingFactory,
    ScheduleInstanceFactory,
)
from quickstart.tasks.payout_tasks import (
    CUTOVER_DATE,
    _build_payout_batch_idempotency_key,
    _build_stripe_transfer_metadata,
    _is_stripe_idempotency_key_conflict,
    process_daily_payouts,
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


def test_build_payout_batch_idempotency_key_is_deterministic_for_same_batch():
    key_a = _build_payout_batch_idempotency_key(
        business_id=99,
        currency="cad",
        booking_ids=[3, 1, 2],
        payout_amount_cents=12345,
    )
    key_b = _build_payout_batch_idempotency_key(
        business_id=99,
        currency="CAD",
        booking_ids=[1, 2, 3],
        payout_amount_cents=12345,
    )
    key_c = _build_payout_batch_idempotency_key(
        business_id=99,
        currency="CAD",
        booking_ids=[1, 2, 4],
        payout_amount_cents=12345,
    )

    assert key_a == key_b
    assert key_a != key_c


def test_build_stripe_transfer_metadata_is_stable_across_retries():
    key = "payout:83:CAD:22236:abc123"
    meta_a = _build_stripe_transfer_metadata(83, 4, key)
    meta_b = _build_stripe_transfer_metadata(83, 4, key)
    assert meta_a == meta_b
    assert "payout_record_id" not in meta_a


def test_is_stripe_idempotency_key_conflict_detects_stripe_message():
    import stripe

    err = stripe.StripeError(
        "Keys for idempotent requests can only be used with the same parameters they were first used with."
    )
    assert _is_stripe_idempotency_key_conflict(err) is True


def _make_payout_booking(business, *, net=Decimal("87.00"), pi_suffix="unique"):
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
        allocated_net_payout=net,
        booking_date=CUTOVER_DATE - timedelta(days=1),
    )
    Payment.objects.create(
        booking=booking,
        stripe_payment_intent_id=f"pi_test_{pi_suffix}",
        amount=Decimal("100.00"),
        status="succeeded",
        stripe_processing_fee=Decimal("3.20"),
        net_payout_amount=net,
    )
    return booking


@pytest.mark.django_db
def test_process_daily_payouts_sends_stable_metadata_to_stripe():
    business = BusinessFactory(
        stripe_account_id="acct_test_stable_metadata",
        currency="CAD",
    )
    booking = _make_payout_booking(business, pi_suffix="stable_metadata")

    transfer = MagicMock()
    transfer.id = "tr_test_stable_metadata_001"
    transfer.currency = "cad"
    transfer.metadata = SimpleNamespace(
        to_dict=lambda: {
            "business_id": str(business.businessId),
            "booking_count": "1",
        }
    )

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create", return_value=transfer) as mock_create:
            summary = process_daily_payouts()

    assert "Failed: 0" in summary
    kwargs = mock_create.call_args.kwargs
    assert "payout_record_id" not in kwargs["metadata"]
    assert kwargs["metadata"]["business_id"] == str(business.businessId)
    assert "batch_idempotency_key" in kwargs["metadata"]
    booking.refresh_from_db()
    assert booking.payout_status == "processed"


@pytest.mark.django_db
def test_process_daily_payouts_recovers_existing_stripe_transfer_on_idempotency_conflict():
    business = BusinessFactory(
        stripe_account_id="acct_test_idem_recover",
        currency="CAD",
    )
    booking = _make_payout_booking(business, pi_suffix="idem_recover")

    recovered = MagicMock()
    recovered.id = "tr_test_idem_recovered_001"
    recovered.currency = "cad"
    recovered.metadata = SimpleNamespace(
        to_dict=lambda: {
            "business_id": str(business.businessId),
            "booking_count": "1",
            "batch_idempotency_key": "placeholder",
        }
    )

    import stripe

    idem_err = stripe.StripeError(
        "Keys for idempotent requests can only be used with the same parameters they were first used with."
    )

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create", side_effect=idem_err):
            with patch(
                "quickstart.tasks.payout_tasks._find_stripe_transfer_for_batch",
                return_value=recovered,
            ):
                summary = process_daily_payouts()

    assert "Failed: 0" in summary
    booking.refresh_from_db()
    assert booking.payout_status == "processed"
    payout = Payout.objects.get(stripe_transfer_id="tr_test_idem_recovered_001")
    assert payout.metadata.get("payout_record_id") == str(payout.id)


@pytest.mark.django_db
def test_process_daily_payouts_retries_with_recovery_key_when_transfer_not_found():
    business = BusinessFactory(
        stripe_account_id="acct_test_idem_v2",
        currency="CAD",
    )
    booking = _make_payout_booking(business, pi_suffix="idem_v2")

    transfer = MagicMock()
    transfer.id = "tr_test_idem_v2_001"
    transfer.currency = "cad"
    transfer.metadata = SimpleNamespace(to_dict=lambda: {"business_id": str(business.businessId)})

    import stripe

    idem_err = stripe.StripeError(
        "Keys for idempotent requests can only be used with the same parameters they were first used with."
    )

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch(
            "stripe.Transfer.create",
            side_effect=[idem_err, transfer],
        ) as mock_create:
            with patch(
                "quickstart.tasks.payout_tasks._find_stripe_transfer_for_batch",
                return_value=None,
            ):
                summary = process_daily_payouts()

    assert "Failed: 0" in summary
    assert mock_create.call_count == 2
    assert mock_create.call_args_list[1].kwargs["idempotency_key"].endswith(":v2")
    booking.refresh_from_db()
    assert booking.payout_status == "processed"


@pytest.mark.django_db
def test_process_daily_payouts_reconciles_pending_bookings_to_existing_payout():
    business = BusinessFactory(
        stripe_account_id="acct_test_existing_payout",
        currency="CAD",
    )
    booking = _make_payout_booking(business, pi_suffix="existing_payout")
    idempotency_key = _build_payout_batch_idempotency_key(
        business_id=business.businessId,
        currency=business.currency,
        booking_ids=[booking.id],
        payout_amount_cents=int(Decimal("87.00") * 100),
    )
    existing = Payout.objects.create(
        business=business,
        stripe_transfer_id="tr_test_existing_payout_001",
        amount=Decimal("87.00"),
        currency="CAD",
        arrival_date=timezone.now().date(),
        status="paid",
        metadata={"batch_idempotency_key": idempotency_key},
    )

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create") as mock_create:
            summary = process_daily_payouts()

    assert "Failed: 0" in summary
    mock_create.assert_not_called()
    booking.refresh_from_db()
    assert booking.payout_status == "processed"
    assert existing.bookings.filter(id=booking.id).exists()


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
    # allocated_net_payout already includes Stripe fee deduction at payment time
    expected_net = Decimal("87.00")
    booking = BookingFactory(
        schedule_instance=inst,
        status="completed",
        payment_status="paid",
        payout_status="pending",
        amount_paid=Decimal("100.00"),
        allocated_net_payout=expected_net,
        booking_date=CUTOVER_DATE - timedelta(days=1),
    )
    Payment.objects.create(
        booking=booking,
        stripe_payment_intent_id="pi_test_meta_merge_unique",
        amount=Decimal("100.00"),
        status="succeeded",
        stripe_processing_fee=Decimal("3.20"),
        net_payout_amount=expected_net,
    )

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
