from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from decimal import Decimal
import stripe
import uuid

from quickstart.models import GiftCard
from quickstart.serializers.public.public_giftcard_serializers import (
    GiftCardValidationSerializer,
    GiftCardPurchaseSerializer,
)

stripe.api_key = settings.STRIPE_SECRET_KEY


class CreateGiftCardPaymentIntentView(APIView):
    """
    Step 1 of buying a gift card: Get Stripe Client Secret.
    """

    permission_classes = []  # Public access

    def post(self, request):
        serializer = GiftCardPurchaseSerializer(data=request.data)
        if serializer.is_valid():
            data = serializer.validated_data
            amount = data["amount"]
            design_url = request.data.get("design_url", "")

            # Metadata is crucial for the webhook to know what to create
            metadata = {
                "type": "gift_card_purchase",
                "recipient_email": data["recipient_email"],
                "recipient_name": data["recipient_name"],
                "sender_name": data["sender_name"],
                "message": data.get("message", ""),
                "is_scheduled": str(bool(data.get("date"))),
                "scheduled_date": str(data.get("date")) if data.get("date") else "",
                "design_url": design_url,
            }

            try:
                # Create Stripe Intent
                intent = stripe.PaymentIntent.create(
                    amount=int(amount * 100),  # Cents
                    currency="cad",
                    automatic_payment_methods={"enabled": True},
                    metadata=metadata,
                    description=f"Gift Card for {data['recipient_name']}",
                )

                return Response(
                    {"clientSecret": intent.client_secret, "amount": amount}
                )
            except Exception as e:
                return Response(
                    {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ValidateGiftCardView(APIView):
    """
    Used in ReviewAndPaymentStep to check balance.
    """

    permission_classes = []

    def post(self, request):
        code = request.data.get("code", "").strip()

        try:
            # Case insensitive lookup
            gift_card = GiftCard.objects.get(code__iexact=code, is_active=True)

            if gift_card.current_balance <= 0:
                return Response(
                    {"error": "This gift card has a zero balance."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response(
                {
                    "code": gift_card.code,  # Return normalized code
                    "balance": gift_card.current_balance,
                }
            )
        except GiftCard.DoesNotExist:
            return Response(
                {"error": "Invalid gift card code."}, status=status.HTTP_404_NOT_FOUND
            )
