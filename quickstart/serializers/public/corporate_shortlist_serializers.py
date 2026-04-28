"""Public (token) serializers for corporate shortlist — no internal fields."""

from rest_framework import serializers


class PublicCorporateInquirySerializer(serializers.Serializer):
    company_name = serializers.CharField()
    contact_name = serializers.CharField()


class PublicCorporateShortlistOptionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    position = serializers.IntegerField()
    source_type = serializers.CharField()
    title = serializers.CharField()
    host_name = serializers.CharField()
    tagline = serializers.CharField()
    description = serializers.CharField()
    inclusions = serializers.ListField(child=serializers.CharField(), required=False)
    cover_image_url = serializers.CharField(allow_blank=True)
    gallery_urls = serializers.ListField(child=serializers.CharField(), required=False)
    location_text = serializers.CharField(allow_blank=True)
    duration_minutes = serializers.IntegerField(allow_null=True)
    min_headcount = serializers.IntegerField(allow_null=True)
    max_headcount = serializers.IntegerField(allow_null=True)
    price_total_cents = serializers.IntegerField()
    price_per_person_cents = serializers.IntegerField(allow_null=True)
    proposed_date_options = serializers.ListField(
        child=serializers.CharField(), required=False
    )


class PublicCorporateBookingSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    reference = serializers.CharField()
    status = serializers.CharField()
    headcount = serializers.IntegerField()
    confirmed_datetime = serializers.DateTimeField()
    total_cents = serializers.IntegerField()
    deposit_cents = serializers.IntegerField()
    balance_cents = serializers.IntegerField()
    currency = serializers.CharField()
    deposit_paid_at = serializers.DateTimeField(allow_null=True)
    invoice_url = serializers.CharField(allow_blank=True)
    invoice_status = serializers.CharField(allow_blank=True)
    invoice_due_at = serializers.DateTimeField(allow_null=True)
    balance_paid_at = serializers.DateTimeField(allow_null=True)
    selected_option_id = serializers.UUIDField(source="selected_option_id", required=False)


class PublicCorporateShortlistDetailSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    token = serializers.UUIDField()
    status = serializers.CharField()
    intro_message = serializers.CharField()
    deposit_percent = serializers.IntegerField()
    currency = serializers.CharField()
    inquiry = PublicCorporateInquirySerializer()
    options = PublicCorporateShortlistOptionSerializer(many=True)
    active_booking = PublicCorporateBookingSerializer(allow_null=True)


class CorporateSelectOptionSerializer(serializers.Serializer):
    option_id = serializers.UUIDField()
    confirmed_datetime = serializers.DateTimeField()
    headcount = serializers.IntegerField(min_value=1)
    special_requests = serializers.CharField(
        max_length=8000, allow_blank=True, required=False, default=""
    )
    billing_company_name = serializers.CharField(max_length=200)
    billing_contact_name = serializers.CharField(max_length=150)
    billing_email = serializers.EmailField()
    billing_address = serializers.JSONField(required=False, default=dict)
    po_number = serializers.CharField(
        max_length=100, allow_blank=True, required=False, default=""
    )
