from rest_framework import serializers

from quickstart.models import CorporateInquiry

_REQUIRED = {"required": "This field is required."}


class CorporateInquiryCreateSerializer(serializers.ModelSerializer):
    """Public POST for /api/corporate-inquiry/ — JSON keys must be snake_case."""

    company_name = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        error_messages=_REQUIRED,
    )
    contact_name = serializers.CharField(
        max_length=150,
        trim_whitespace=True,
        error_messages=_REQUIRED,
    )
    email = serializers.EmailField(
        max_length=254,
        error_messages={**_REQUIRED, "invalid": "Enter a valid email address."},
    )
    phone = serializers.CharField(
        max_length=50,
        required=False,
        allow_blank=True,
        default="",
        trim_whitespace=True,
    )
    company_size = serializers.ChoiceField(
        choices=CorporateInquiry.COMPANY_SIZE_CHOICES,
        required=False,
        allow_blank=True,
        default="",
    )
    message = serializers.CharField(
        max_length=5000,
        required=False,
        allow_blank=True,
        default="",
        trim_whitespace=True,
    )
    meta = serializers.JSONField(required=False, default=dict)

    class Meta:
        model = CorporateInquiry
        fields = [
            "company_name",
            "contact_name",
            "email",
            "phone",
            "company_size",
            "message",
            "meta",
        ]

    def create(self, validated_data):
        request = self.context.get("request")
        meta = validated_data.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        if request is not None:
            meta = {
                **meta,
                "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:500],
            }
        validated_data["meta"] = meta
        return super().create(validated_data)

    def validate_company_name(self, value):
        v = (value or "").strip()
        if len(v) < 2:
            raise serializers.ValidationError("Please enter a company name.")
        return v

    def validate_contact_name(self, value):
        v = (value or "").strip()
        if len(v) < 2:
            raise serializers.ValidationError("Please enter your name.")
        return v

    def validate_message(self, value):
        if value and len(value) > 5000:
            raise serializers.ValidationError("Message is too long.")
        return (value or "").strip()
