from decimal import Decimal

from rest_framework import serializers
from quickstart.models import GiftCard


class GiftCardValidationSerializer(serializers.Serializer):
    code = serializers.CharField(required=True)


class GiftCardPurchaseSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("5.00")
    )
    recipient_email = serializers.EmailField()
    recipient_name = serializers.CharField()
    sender_name = serializers.CharField()
    message = serializers.CharField(allow_blank=True, required=False)
    date = serializers.DateField(allow_null=True, required=False) # For scheduled sending
    delivery_method = serializers.CharField(default="email") # email or self