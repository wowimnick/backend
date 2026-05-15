"""Admin serializers for corporate inquiries, shortlists, options, bookings."""

from django.utils import timezone
from rest_framework import serializers

from quickstart.models import (
    CorporateBooking,
    CorporateBookingEvent,
    CorporateInquiry,
    CorporateShortlist,
    CorporateShortlistOption,
    ClassesMain,
)


class CorporateInquiryListSerializer(serializers.ModelSerializer):
    shortlist_id = serializers.UUIDField(
        source="shortlist.id", read_only=True, allow_null=True
    )
    shortlist_status = serializers.CharField(
        source="shortlist.status", read_only=True, allow_null=True
    )
    has_booking = serializers.SerializerMethodField()

    class Meta:
        model = CorporateInquiry
        fields = [
            "id",
            "company_name",
            "contact_name",
            "email",
            "phone",
            "company_size",
            "message",
            "meta",
            "created_at",
            "shortlist_id",
            "shortlist_status",
            "has_booking",
        ]

    def get_has_booking(self, obj):
        s = getattr(obj, "shortlist", None)
        if not s:
            return False
        from quickstart.models import CorporateBooking
        return (
            s.bookings.exclude(
                status__in=[
                    CorporateBooking.ST_CANCELLED,
                    CorporateBooking.ST_REFUNDED,
                ]
            ).exists()
        )


class CorporateInquiryAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorporateInquiry
        fields = "__all__"


class CorporateShortlistOptionWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorporateShortlistOption
        fields = [
            "position",
            "source_type",
            "source_class",
            "title",
            "host_name",
            "tagline",
            "description",
            "inclusions",
            "cover_image_url",
            "gallery_urls",
            "location_text",
            "duration_minutes",
            "min_headcount",
            "max_headcount",
            "price_total_cents",
            "price_per_person_cents",
            "proposed_date_options",
            "is_archived",
        ]

    def validate_position(self, value):
        if value < 1 or value > 3:
            raise serializers.ValidationError("Position must be 1, 2, or 3.")
        return value


class CorporateShortlistOptionReadSerializer(serializers.ModelSerializer):
    source_class_slug = serializers.SerializerMethodField()
    gallery_urls = serializers.SerializerMethodField()
    location_label = serializers.SerializerMethodField()
    maps_query = serializers.SerializerMethodField()

    class Meta:
        model = CorporateShortlistOption
        fields = [
            "id",
            "shortlist",
            "position",
            "source_type",
            "source_class",
            "source_class_slug",
            "title",
            "host_name",
            "tagline",
            "description",
            "inclusions",
            "cover_image_url",
            "gallery_urls",
            "location_text",
            "location_label",
            "maps_query",
            "duration_minutes",
            "min_headcount",
            "max_headcount",
            "price_total_cents",
            "price_per_person_cents",
            "proposed_date_options",
            "is_archived",
            "created_at",
            "updated_at",
        ]

    def get_source_class_slug(self, obj):
        sc = obj.source_class
        return getattr(sc, "slug", None) if sc else None

    def get_gallery_urls(self, obj):
        from quickstart.utils.corporate_shortlist_images import (
            effective_gallery_urls_for_option,
        )

        return effective_gallery_urls_for_option(obj)

    def get_location_label(self, obj):
        from quickstart.utils.corporate_shortlist_location import (
            location_label_for_option,
        )

        return location_label_for_option(obj)

    def get_maps_query(self, obj):
        from quickstart.utils.corporate_shortlist_location import (
            maps_search_query_for_option,
        )

        return maps_search_query_for_option(obj)


class CorporateShortlistAdminSerializer(serializers.ModelSerializer):
    options = CorporateShortlistOptionReadSerializer(many=True, read_only=True)

    class Meta:
        model = CorporateShortlist
        fields = [
            "id",
            "inquiry",
            "token",
            "status",
            "intro_message",
            "internal_notes",
            "deposit_percent",
            "currency",
            "sent_at",
            "first_viewed_at",
            "last_viewed_at",
            "created_at",
            "updated_at",
            "options",
        ]
        read_only_fields = [
            "id",
            "inquiry",
            "token",
            "status",
            "sent_at",
            "first_viewed_at",
            "last_viewed_at",
            "created_at",
            "updated_at",
            "options",
        ]


class CorporateBookingEventSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(
        source="created_by.email", read_only=True, allow_null=True
    )

    class Meta:
        model = CorporateBookingEvent
        fields = [
            "id",
            "event_type",
            "message",
            "metadata",
            "created_by",
            "created_by_email",
            "created_at",
        ]


class CorporateBookingAdminSerializer(serializers.ModelSerializer):
    events = CorporateBookingEventSerializer(many=True, read_only=True)
    company_name = serializers.CharField(
        source="shortlist.inquiry.company_name", read_only=True
    )
    shortlist_token = serializers.UUIDField(
        source="shortlist.token", read_only=True
    )
    selected_option = CorporateShortlistOptionReadSerializer(read_only=True)

    class Meta:
        model = CorporateBooking
        fields = [
            "id",
            "reference",
            "status",
            "shortlist",
            "shortlist_token",
            "company_name",
            "selected_option",
            "headcount",
            "confirmed_datetime",
            "special_requests",
            "billing_company_name",
            "billing_contact_name",
            "billing_email",
            "billing_address",
            "po_number",
            "total_cents",
            "deposit_cents",
            "balance_cents",
            "currency",
            "stripe_customer_id",
            "deposit_payment_intent_id",
            "deposit_paid_at",
            "stripe_invoice_id",
            "invoice_status",
            "invoice_url",
            "invoice_due_at",
            "balance_paid_at",
            "created_at",
            "updated_at",
            "events",
        ]
        read_only_fields = [
            "reference",
            "stripe_customer_id",
            "deposit_payment_intent_id",
            "deposit_paid_at",
            "stripe_invoice_id",
            "invoice_status",
            "invoice_url",
            "invoice_due_at",
            "balance_paid_at",
        ]


class AddOptionFromClassSerializer(serializers.Serializer):
    """Body for POST .../options/from-class/ — hydrates from ClassesMain."""

    position = serializers.IntegerField(min_value=1, max_value=3)
    class_id = serializers.IntegerField()
    price_total_cents = serializers.IntegerField(min_value=1)
    price_per_person_cents = serializers.IntegerField(
        min_value=0, allow_null=True, required=False
    )
    host_name = serializers.CharField(
        max_length=200, allow_blank=True, required=False, default=""
    )
    inclusions = serializers.ListField(
        child=serializers.CharField(max_length=500), required=False, default=list
    )
    proposed_date_options = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    title_override = serializers.CharField(
        max_length=200, allow_blank=True, required=False, default=""
    )
    description_override = serializers.CharField(
        max_length=8000, allow_blank=True, required=False, default=""
    )
    cover_image_url_override = serializers.CharField(
        allow_blank=True, required=False, default=""
    )
    tagline = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default=""
    )
    duration_minutes = serializers.IntegerField(
        allow_null=True, required=False, min_value=1
    )
    min_headcount = serializers.IntegerField(
        allow_null=True, required=False, min_value=1
    )
    max_headcount = serializers.IntegerField(
        allow_null=True, required=False, min_value=1
    )

    def validate(self, data):
        try:
            cls = ClassesMain.objects.select_related("businessId").get(
                classId=data["class_id"]
            )
        except ClassesMain.DoesNotExist as e:
            raise serializers.ValidationError({"class_id": "Class not found."}) from e
        data["_class"] = cls
        return data


class IssueInvoiceSerializer(serializers.Serializer):
    due_in_days = serializers.IntegerField(min_value=1, max_value=90, default=15)
