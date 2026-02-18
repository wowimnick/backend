"""
Serializers for guest–business conversations (business / dashboard side).
"""

from rest_framework import serializers

from quickstart.models import Conversation, ConversationMessage


class BusinessConversationMessageSerializer(serializers.ModelSerializer):
    """Read-only message for business list/detail."""

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


class BusinessConversationMessageCreateSerializer(serializers.Serializer):
    """Business sends a reply."""

    text = serializers.CharField(max_length=5000, trim_whitespace=True)

    def validate_text(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Message text is required.")
        return value.strip()


class BusinessConversationListSerializer(serializers.ModelSerializer):
    """List item for business: conversation with guest name and last message preview."""

    booker_display = serializers.SerializerMethodField()
    booker_email = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()
    booking_reference = serializers.SerializerMethodField()
    class_title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "business",
            "booking",
            "booking_reference",
            "class_title",
            "booker_display",
            "booker_email",
            "last_message_at",
            "last_message_preview",
            "created_at",
        ]

    def get_booker_display(self, obj):
        if obj.booker_user:
            return obj.booker_user.get_full_name() or obj.booker_user.email
        if obj.booker_contact:
            return f"{obj.booker_contact.first_name} {obj.booker_contact.last_name}".strip() or (obj.booker_contact.email or "Guest")
        return "Unknown"

    def get_booker_email(self, obj):
        if obj.booker_user:
            return obj.booker_user.email
        if obj.booker_contact:
            return obj.booker_contact.email
        return None

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


class BusinessConversationDetailSerializer(serializers.ModelSerializer):
    """Detail for business: conversation with nested messages."""

    booker_display = serializers.SerializerMethodField()
    booker_email = serializers.SerializerMethodField()
    messages = BusinessConversationMessageSerializer(many=True, read_only=True)
    booking_reference = serializers.SerializerMethodField()
    class_title = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "business",
            "booking",
            "booking_reference",
            "class_title",
            "booker_display",
            "booker_email",
            "last_message_at",
            "created_at",
            "messages",
        ]

    def get_booker_display(self, obj):
        if obj.booker_user:
            return obj.booker_user.get_full_name() or obj.booker_user.email
        if obj.booker_contact:
            return f"{obj.booker_contact.first_name} {obj.booker_contact.last_name}".strip() or (obj.booker_contact.email or "Guest")
        return "Unknown"

    def get_booker_email(self, obj):
        if obj.booker_user:
            return obj.booker_user.email
        if obj.booker_contact:
            return obj.booker_contact.email
        return None

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
