"""
Read reversal state from Stripe Transfer objects (Connect).
https://docs.stripe.com/api/transfers/object
"""

from decimal import Decimal


def stripe_transfer_reversal_fields(tr) -> dict:
    """
    Return cents + human labels for how much of the transfer was reversed.
    """
    amount = int(getattr(tr, "amount", 0) or 0)
    amount_reversed = int(getattr(tr, "amount_reversed", 0) or 0)
    remaining = max(0, amount - amount_reversed)

    if amount_reversed <= 0:
        label = "not_reversed"
    elif remaining == 0:
        label = "fully_reversed"
    else:
        label = "partially_reversed"

    def _d(cents: int) -> Decimal:
        return (Decimal(int(cents)) / Decimal(100)).quantize(Decimal("0.01"))

    return {
        "amount_cents": amount,
        "amount_reversed_cents": amount_reversed,
        "remaining_cents": remaining,
        "amount_dollars": _d(amount),
        "amount_reversed_dollars": _d(amount_reversed),
        "remaining_dollars": _d(remaining),
        "label": label,
    }


def merge_reversal_into_orphan(orphan: dict, tr) -> dict:
    """Mutate orphan dict with reversal_* keys from live Stripe object."""
    r = stripe_transfer_reversal_fields(tr)
    orphan["reversal_label"] = r["label"]
    orphan["amount_reversed_dollars"] = r["amount_reversed_dollars"]
    orphan["remaining_dollars"] = r["remaining_dollars"]
    orphan["amount_cents"] = r["amount_cents"]
    orphan["amount_reversed_cents"] = r["amount_reversed_cents"]
    return orphan
