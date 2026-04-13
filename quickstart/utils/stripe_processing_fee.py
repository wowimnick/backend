"""Stripe card processing fee estimate (matches payout_tasks legacy formula)."""
from decimal import Decimal

STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_FIXED = Decimal("0.30")


def estimate_stripe_processing_fee(charge_amount):
    """
    Estimate Stripe processing fee for a charge (2.9% + $0.30).
    charge_amount is the gross charged to the customer (same currency as payment.amount).
    """
    if charge_amount is None or charge_amount <= 0:
        return Decimal("0.00")
    fee = (charge_amount * STRIPE_FEE_PERCENT + STRIPE_FEE_FIXED).quantize(
        Decimal("0.01")
    )
    return fee
