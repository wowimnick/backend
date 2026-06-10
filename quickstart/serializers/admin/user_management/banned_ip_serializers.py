from rest_framework import serializers

from quickstart.models import BannedIP


class BannedIPSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(
        source="created_by.email", read_only=True, allow_null=True
    )

    class Meta:
        model = BannedIP
        fields = [
            "id",
            "ip_address",
            "reason",
            "created_by",
            "created_by_email",
            "created_at",
            "expires_at",
            "is_active",
        ]
        read_only_fields = ["id", "created_by", "created_by_email", "created_at"]
