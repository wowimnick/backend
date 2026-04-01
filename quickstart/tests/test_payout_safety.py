"""
Regression tests for payout pipeline safety (metadata merge, backfill email skip).

Run: pytest quickstart/tests/test_payout_safety.py -v
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from quickstart.models import Payout
from quickstart.tests.factories import BusinessFactory
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict


def test_stripe_metadata_to_dict_none():
    assert stripe_metadata_to_dict(None) == {}


def test_stripe_metadata_to_dict_plain_dict():
    assert stripe_metadata_to_dict({"k": "v"}) == {"k": "v"}


def test_stripe_metadata_to_dict_uses_to_dict():
    class M:
        def to_dict(self):
            return {"business_id": "45"}

    assert stripe_metadata_to_dict(M()) == {"business_id": "45"}


def test_stripe_metadata_to_dict_fallback_items():
    class M:
        def items(self):
            return [("x", "y")]

    assert stripe_metadata_to_dict(M()) == {"x": "y"}


def _on_commit_run_now(func, using=None):
    """Match Django's on_commit signature; run callback now (tests use rolled-back transactions)."""
    func()


@pytest.mark.django_db
def test_payout_created_without_skip_queues_initiated_email():
    business = BusinessFactory(
        stripe_account_id="acct_test_payout_sig",
        currency="CAD",
    )
    with patch("django.db.transaction.on_commit", _on_commit_run_now):
        with patch("quickstart.utils.email_utils.send_templated_email") as mock_tpl:
            Payout.objects.create(
                business=business,
                stripe_transfer_id="tr_test_sig_normal",
                amount=Decimal("10.00"),
                currency="CAD",
                status="paid",
                metadata={"source": "unit_test"},
            )
    assert mock_tpl.call_count >= 1


@pytest.mark.django_db
def test_payout_created_with_skip_does_not_queue_initiated_email():
    business = BusinessFactory(
        stripe_account_id="acct_test_payout_sig_skip",
        currency="CAD",
    )
    with patch("django.db.transaction.on_commit", _on_commit_run_now):
        with patch("quickstart.utils.email_utils.send_templated_email") as mock_tpl:
            Payout.objects.create(
                business=business,
                stripe_transfer_id="tr_test_sig_skip",
                amount=Decimal("10.00"),
                currency="CAD",
                status="paid",
                metadata={
                    "source": "backfill_payout_from_stripe_transfer",
                    "skip_payout_notification": True,
                },
            )
    mock_tpl.assert_not_called()
