"""Stripe refund helper that reverses destination-charge transfers when possible."""

import logging

import stripe

logger = logging.getLogger(__name__)


def create_stripe_refund(**kwargs):
    """
    Create a Stripe refund. For destination charges, reverse the transfer and
    application fee. If Stripe rejects those flags (legacy platform charge),
    retry without them.
    """
    try:
        return stripe.Refund.create(
            reverse_transfer=True,
            refund_application_fee=True,
            **kwargs,
        )
    except stripe.StripeError as e:
        logger.info(
            "Refund with reverse_transfer/refund_application_fee rejected; retrying without: %s",
            e,
        )
        return stripe.Refund.create(**kwargs)
