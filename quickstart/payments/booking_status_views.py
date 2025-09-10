# quickstart/views/bookings/booking_status_views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from quickstart.models import Payment, Booking
import stripe  # Import stripe
from django.conf import settings  # Import settings

import logging

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class BookingStatusByPaymentIntentView(APIView):
    # Allow unauthenticated access, as guests will use this endpoint.
    permission_classes = []

    def get(self, request, payment_intent_id):
        if not payment_intent_id:
            return Response(
                {"error": "Payment Intent ID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- MODIFICATION START ---
        # For guests, we require the client_secret as a temporary auth token.
        is_guest = not request.user or not request.user.is_authenticated
        client_secret = request.query_params.get("client_secret")

        if is_guest and not client_secret:
            logger.warning(
                f"Guest status check for PI {payment_intent_id} failed: missing client_secret."
            )
            return Response(
                {"error": "Authorization required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        # --- MODIFICATION END ---

        try:
            payment = (
                Payment.objects.select_related("booking")
                .filter(stripe_payment_intent_id=payment_intent_id)
                .first()
            )

            if not payment:
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment record not found yet (webhook might be pending)."
                )
                return Response(
                    {
                        "status": "pending_webhook",
                        "message": "Booking confirmation is processing.",
                    },
                    status=status.HTTP_202_ACCEPTED,
                )

            # --- MODIFICATION START: Updated Security Check ---
            # Now we verify the owner of the booking in two ways:
            # 1. If a user is logged in, they must be the owner of the booking.
            # 2. If it's a guest, the provided client_secret must match the one from Stripe.

            is_authorized = False
            if not is_guest:
                # Logged-in user check
                if payment.booking and payment.booking.user == request.user:
                    is_authorized = True
            else:
                # Guest check using client_secret
                try:
                    retrieved_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
                    if retrieved_intent.client_secret == client_secret:
                        is_authorized = True
                except stripe.error.StripeError as e:
                    logger.error(
                        f"Stripe API error checking client_secret for PI {payment_intent_id}: {e}"
                    )

            if not is_authorized:
                logger.warning(
                    f"User/Guest attempted to access booking status for PI {payment_intent_id} without authorization."
                )
                return Response(
                    {"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN
                )
            # --- MODIFICATION END ---

            if payment.status == "succeeded" and payment.booking:
                booking = payment.booking
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Found successful payment and booking {booking.id} (Ref: {booking.user_facing_reference})."
                )
                return Response(
                    {
                        "status": "confirmed",
                        "booking_id": booking.id,
                        "user_facing_reference": booking.user_facing_reference,
                        "booking_group_id": (
                            str(booking.booking_group_id)
                            if booking.booking_group_id
                            else None
                        ),
                        "participant_details": booking.participant_details,
                        "message": "Booking confirmed.",
                    },
                    status=status.HTTP_200_OK,
                )
            elif payment.status == "failed":
                logger.warning(
                    f"Booking status check for PI {payment_intent_id}: Payment failed."
                )
                return Response(
                    {
                        "status": "payment_failed",
                        "message": "Payment processing failed.",
                        "failure_message": payment.failure_message,
                    },
                    status=status.HTTP_200_OK,
                )
            else:
                logger.info(
                    f"Booking status check for PI {payment_intent_id}: Payment status is '{payment.status}'."
                )
                return Response(
                    {
                        "status": "processing",
                        "message": f"Booking confirmation is still processing (Payment status: {payment.status}).",
                    },
                    status=status.HTTP_202_ACCEPTED,
                )

        except Exception as e:
            logger.error(
                f"Error fetching booking status for PI {payment_intent_id}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while fetching booking status."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
