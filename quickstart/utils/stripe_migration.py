"""Legacy Stripe Elements / SetupIntent endpoints — use Checkout + Customer Portal instead."""

from rest_framework import status
from rest_framework.response import Response


def stripe_migration_gone_response():
    """410 — inline payment/setup flows were replaced by Checkout and the Customer Portal."""
    return Response(
        {
            "detail": "This billing flow is retired. Use Stripe Checkout and the Customer Portal from Plan & billing.",
            "code": "stripe_checkout_migration",
        },
        status=status.HTTP_410_GONE,
    )
