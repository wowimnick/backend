from rest_framework import serializers

from quickstart.models import CorporateInquiry


class CorporateInquiryCreateSerializer(serializers.ModelSerializer):
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
