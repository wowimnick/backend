from rest_framework import serializers
from quickstart.models import GlobalDiscount
from django.db.models import Sum, Count


class AdminGlobalDiscountSerializer(serializers.ModelSerializer):
    """CRUD serializer for GlobalDiscount in admin."""

    class Meta:
        model = GlobalDiscount
        fields = [
            "id",
            "name",
            "discount_type",
            "value",
            "is_active",
            "valid_from",
            "valid_to",
            "usage_limit",
            "usage_count",
            "min_purchase_amount",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "usage_count", "created_at", "updated_at"]


class AdminGlobalDiscountStatsSerializer(serializers.Serializer):
    """Read-only stats for a GlobalDiscount."""

    usage_count = serializers.IntegerField()
    total_amount_saved = serializers.DecimalField(max_digits=12, decimal_places=2)
    bookings_count = serializers.IntegerField()
