from decimal import Decimal
from unittest.mock import patch

import pytest

from quickstart.tasks.payout_tasks import (
    collect_payout_integrity_findings,
    monitor_payout_integrity,
    send_daily_payout_integrity_warning_digest,
)
from quickstart.tests.factories import BusinessFactory, PayoutFactory


@pytest.mark.django_db
def test_collect_payout_integrity_findings_detects_duplicates_and_temp_rows():
    business = BusinessFactory(stripe_account_id="acct_monitor_test_001")
    PayoutFactory(
        business=business,
        amount=Decimal("25.00"),
        currency="CAD",
        stripe_transfer_id="tr_dup_1",
        status="paid",
    )
    PayoutFactory(
        business=business,
        amount=Decimal("25.00"),
        currency="CAD",
        stripe_transfer_id="tr_dup_2",
        status="paid",
    )
    PayoutFactory(
        business=business,
        amount=Decimal("40.00"),
        currency="CAD",
        stripe_transfer_id="temp_task_999",
        metadata={"temp_id": True},
        status="pending",
    )

    report = collect_payout_integrity_findings(include_stripe=False)

    assert len(report["duplicate_like_groups"]) == 1
    assert len(report["temp_like_rows"]) == 1
    assert any(
        "Duplicate-like payout groups" in item for item in report["critical_findings"]
    )
    assert any(
        "temporary transfer ids" in item for item in report["critical_findings"]
    )


def test_monitor_payout_integrity_does_not_email_when_payout_emails_disabled():
    report = {
        "ran_at": "2026-01-01T00:00:00+00:00",
        "days": 3,
        "stripe_limit": 200,
        "findings": ["Duplicate-like payout groups: 1"],
        "critical_findings": ["Duplicate-like payout groups: 1"],
        "warning_findings": [],
        "duplicate_like_groups": [],
        "temp_like_rows": [],
        "orphan_transfers": [],
        "stripe_api_errors": [],
        "pending_queue_top": [],
        "pending_queue_businesses": 0,
        "pending_queue_total_alloc": "0.00",
    }
    with patch(
        "quickstart.tasks.payout_tasks.collect_payout_integrity_findings",
        return_value=report,
    ):
        with patch("quickstart.utils.email_utils.send_templated_email") as mock_send:
            result = monitor_payout_integrity(days=3, stripe_limit=200)

    assert "No alert email sent" in result
    mock_send.assert_not_called()


def test_monitor_payout_integrity_does_not_email_warning_only_findings():
    report = {
        "ran_at": "2026-01-01T00:00:00+00:00",
        "days": 3,
        "stripe_limit": 200,
        "findings": ["Pending payout queue elevated: 30 businesses, total $9000.00"],
        "critical_findings": [],
        "warning_findings": ["Pending payout queue elevated: 30 businesses, total $9000.00"],
        "duplicate_like_groups": [],
        "temp_like_rows": [],
        "orphan_transfers": [],
        "stripe_api_errors": [],
        "pending_queue_top": [],
        "pending_queue_businesses": 30,
        "pending_queue_total_alloc": "9000.00",
    }
    with patch(
        "quickstart.tasks.payout_tasks.collect_payout_integrity_findings",
        return_value=report,
    ):
        with patch("quickstart.utils.email_utils.send_templated_email") as mock_send:
            result = monitor_payout_integrity(days=3, stripe_limit=200)

    assert "No immediate email sent" in result
    mock_send.assert_not_called()


def test_daily_digest_does_not_email_when_payout_emails_disabled():
    report = {
        "ran_at": "2026-01-01T00:00:00+00:00",
        "days": 7,
        "stripe_limit": 300,
        "findings": ["Pending payout queue elevated: 30 businesses, total $9000.00"],
        "critical_findings": [],
        "warning_findings": ["Pending payout queue elevated: 30 businesses, total $9000.00"],
        "duplicate_like_groups": [],
        "temp_like_rows": [],
        "orphan_transfers": [],
        "stripe_api_errors": [],
        "pending_queue_top": [],
        "pending_queue_businesses": 30,
        "pending_queue_total_alloc": "9000.00",
    }
    with patch(
        "quickstart.tasks.payout_tasks.collect_payout_integrity_findings",
        return_value=report,
    ):
        with patch("quickstart.utils.email_utils.send_templated_email") as mock_send:
            result = send_daily_payout_integrity_warning_digest(days=7, stripe_limit=300)

    assert "no digest email sent" in result
    mock_send.assert_not_called()
