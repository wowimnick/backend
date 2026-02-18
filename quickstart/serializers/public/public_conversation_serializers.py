"""
Serializers for guest–business conversations (booker / public side).
"""

from rest_framework import serializers

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
    Booking,
    Contact,
)


class ConversationMessageSerializer(serializers.ModelSerializer):
    """Read-only message for list/detail."""

    sender_display = serializers.SerializerMethodField()

    class Meta:
        model = ConversationMessage
        fields = [
            "id",
            "conversation",
            "sender_type",
            "sender_user",
            "sender_contact",
            "sender_display",
            "text",
            "created_at",
        ]
        read_only_fields = fields

    def get_sender_display(self, obj):
        if obj.sender_type == ConversationMessage.SENDER_BUSINESS and obj.sender_user:
            return obj.sender_user.get_full_name() or obj.sender_user.email
        if obj.sender_type == ConversationMessage.SENDER_BOOKER:
            if obj.sender_user:
                return obj.sender_user.get_full_name() or obj.sender_user.email
            if obj.sender_contact:
                return f"{obj.sender_contact.first_name} {obj.sender_contact.last_name}".strip() or (obj.sender_contact.email or "Guest")
        return "Unknown"


class ConversationMessageCreateSerializer(serializers.Serializer):
    """Booker sends a message."""

    text = serializers.CharField(max_length=5000, trim_whitespace=True)

    def validate_text(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Message text is required.")
        return value.strip()


class ConversationListSerializer(serializers.ModelSerializer):
    """List item: conversation with business name and last message preview."""

    business_name = serializers.CharField(source="business.businessName", read_only=True)
    business_slug = serializers.SlugField(source="business.slug", read_only=True)
    last_message_preview = serializers.SerializerMethodField()
    booking_reference = serializers.SerializerMethodField()
    class_title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "business",
            "business_name",
            "business_slug",
            "booking",
            "booking_reference",
            "class_title",
            "last_message_at",
            "last_message_preview",
            "created_at",
        ]

    def get_last_message_preview(self, obj):
        last = obj.messages.order_by("-created_at").first()
        if not last:
            return None
        text = (last.text or "").strip()
        return (text[:100] + "…") if len(text) > 100 else text if text else None

    def get_booking_reference(self, obj):
        if not obj.booking:
            return None
        return getattr(obj.booking, "user_facing_reference", None) or str(obj.booking.id)

    def get_class_title(self, obj):
        if not obj.booking or not getattr(obj.booking, "schedule_instance", None):
            return None
        try:
            si = obj.booking.schedule_instance
            option = getattr(si.schedule, "option", None) if si else None
            if option and getattr(option, "classId", None):
                return getattr(option.classId, "title", None)
        except Exception:
            pass
        return None


class ConversationDetailSerializer(serializers.ModelSerializer):
    """Detail view: conversation with nested messages (paginated separately in view)."""

    business_name = serializers.CharField(source="business.businessName", read_only=True)
    business_slug = serializers.SlugField(source="business.slug", read_only=True)
    messages = ConversationMessageSerializer(many=True, read_only=True)
    booking_reference = serializers.SerializerMethodField()
    class_title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "business",
            "business_name",
            "business_slug",
            "booking",
            "booking_reference",
            "class_title",
            "last_message_at",
            "created_at",
            "messages",
        ]

    def get_booking_reference(self, obj):
        if not obj.booking:
            return None
        return getattr(obj.booking, "user_facing_reference", None) or str(obj.booking.id)

    def get_class_title(self, obj):
        if not obj.booking or not getattr(obj.booking, "schedule_instance", None):
            return None
        try:
            si = obj.booking.schedule_instance
            option = getattr(si.schedule, "option", None) if si else None
            if option and getattr(option, "classId", None):
                return getattr(option.classId, "title", None)
        except Exception:
            pass
        return None


class ConversationCreateSerializer(serializers.Serializer):
    """Create or get existing conversation. Body: business_id, optional booking_id."""

    business_id = serializers.IntegerField()
    booking_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_business_id(self, value):
        if not BusinessInfo.objects.filter(businessId=value).exists():
            raise serializers.ValidationError("Business not found.")
        return value
