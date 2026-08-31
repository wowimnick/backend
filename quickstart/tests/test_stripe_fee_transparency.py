"""
Stripe fee transparency: stored processing fee, payout task uses allocated_net_payout as-is,
revenue analytics split.

Run: pytest quickstart/tests/test_stripe_fee_transparency.py -v
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from quickstart.models import Payment, Payout
from quickstart.tasks.payout_tasks import CUTOVER_DATE, process_daily_payouts
from quickstart.tests.factories import (
    BusinessFactory,
    BookingFactory,
    ScheduleInstanceFactory,
)
from quickstart.utils.stripe_processing_fee import estimate_stripe_processing_fee
from quickstart.views.business.revenue_analytics_views import RevenueAnalyticsView


def test_estimate_stripe_processing_fee_formula():
    assert estimate_stripe_processing_fee(Decimal("100.00")) == Decimal("3.20")
    assert estimate_stripe_processing_fee(Decimal("0")) == Decimal("0.00")
    assert estimate_stripe_processing_fee(None) == Decimal("0.00")


@pytest.mark.django_db
def test_process_daily_payouts_transfers_allocated_net_without_extra_stripe_deduction():
    business = BusinessFactory(
        stripe_account_id="acct_test_fee_transparency",
        currency="CAD",
    )
    yesterday = timezone.now().date() - timedelta(days=1)
    inst = ScheduleInstanceFactory(
        schedule__option__classId__businessId=business,
        date=yesterday,
    )
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
        stripe_payment_intent_id="pi_test_fee_transparency_001",
        amount=Decimal("100.00"),
        status="succeeded",
        stripe_processing_fee=Decimal("3.20"),
        net_payout_amount=expected_net,
    )

    class MetaObj:
        def to_dict(self):
            return {"business_id": str(business.businessId), "booking_count": "1"}

    transfer = MagicMock()
    transfer.id = "tr_test_fee_transparency_001"
    transfer.currency = "cad"
    transfer.metadata = MetaObj()

    with patch("quickstart.utils.email_utils.send_templated_email"):
        with patch("stripe.Transfer.create", return_value=transfer):
            summary = process_daily_payouts()

    assert "Failed: 0" in summary
    booking.refresh_from_db()
    assert booking.payout_status == "processed"
    payout = Payout.objects.get(stripe_transfer_id="tr_test_fee_transparency_001")
    assert payout.amount == expected_net
    assert int(payout.amount * 100) == 8700


@pytest.mark.django_db
def test_revenue_analytics_splits_platform_commission_and_stripe_processing():
    business = BusinessFactory()
    # Single anchor + padded upper bound: factory/signals can take a long time, so
    # booking_date must not fall after `end` used in booking_date__range.
    now = timezone.now()
    start = now - timedelta(days=7)
    end = now + timedelta(days=1)

    booking = BookingFactory(
        schedule_instance__schedule__option__classId__businessId=business,
        booking_date=now,
        payment_status="paid",
        status="confirmed",
        amount_paid=Decimal("100.00"),
        allocated_net_payout=Decimal("80.00"),
    )
    Payment.objects.create(
        booking=booking,
        stripe_payment_intent_id="pi_test_rev_split_001",
        amount=Decimal("100.00"),
        status="succeeded",
        platform_fee_amount=Decimal("10.00"),
        stripe_processing_fee=Decimal("3.20"),
        net_payout_amount=Decimal("80.00"),
    )

    view = RevenueAnalyticsView()
    metrics = view.calculate_metrics(business, start, end, source="marketplace")
    assert metrics["platform_commission"] == 10.0
    assert metrics["stripe_processing_fees"] == 3.2
    assert metrics["estimated_net_revenue"] == 80.0
    assert abs(metrics["estimated_platform_fees"] - 13.2) < 0.01
