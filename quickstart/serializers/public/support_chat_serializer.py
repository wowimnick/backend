import re
from rest_framework import serializers
from django.db import transaction

from quickstart.models import ChatMessage, ChatSession, CustomUser, SupportTicket


class UserBriefSerializer(serializers.ModelSerializer):
    """Brief user information serializer"""

    full_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ["userId", "email", "full_name", "avatar_url"]

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"

    def get_avatar_url(self, obj):
        if obj.avatar:
            return obj.avatar.url
        return None


class ChatMessageSerializer(serializers.ModelSerializer):
    """Serializer for chat messages"""

    isUser = serializers.BooleanField(source="is_user")
    timestamp = serializers.DateTimeField(source="created_at")
    text = serializers.CharField(source="content")
    senderType = serializers.CharField(source="sender_type")

    class Meta:
        model = ChatMessage
        fields = ["id", "text", "isUser", "timestamp", "senderType"]


class SupportTicketSerializer(serializers.ModelSerializer):
    """Basic serializer for support tickets"""

    user_details = UserBriefSerializer(source="user", read_only=True)
    assigned_to_details = UserBriefSerializer(source="assigned_to", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )

    class Meta:
        model = SupportTicket
        fields = [
            "ticket_id",
            "subject",
            "category",
            "category_display",
            "description",
            "status",
            "status_display",
            "priority",
            "priority_display",
            "created_at",
            "updated_at",
            "user",
            "user_details",
            "assigned_to",
            "assigned_to_details",
            "resolution_notes",
        ]
        read_only_fields = ["ticket_id", "created_at", "updated_at"]


class ChatSessionSerializer(serializers.ModelSerializer):
    """Serializer for chat sessions with messages"""

    messages = ChatMessageSerializer(many=True, read_only=True)

    class Meta:
        model = ChatSession
        fields = ["id", "userId", "created_at", "updated_at", "messages"]
        read_only_fields = ["id", "created_at", "updated_at"]


class SupportTicketDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for support tickets including conversation"""

    user_details = UserBriefSerializer(source="user", read_only=True)
    assigned_to_details = UserBriefSerializer(source="assigned_to", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    conversation = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = [
            "ticket_id",
            "subject",
            "category",
            "category_display",
            "description",
            "status",
            "status_display",
            "priority",
            "priority_display",
            "created_at",
            "updated_at",
            "user",
            "user_details",
            "assigned_to",
            "assigned_to_details",
            "resolution_notes",
            "conversation",
        ]
        read_only_fields = ["ticket_id", "created_at", "updated_at"]

    def get_conversation(self, obj):
        """Get conversation messages if chat session exists"""
        if not obj.chat_session:
            return []

        messages = obj.chat_session.messages.all().order_by("created_at")
        return ChatMessageSerializer(messages, many=True).data


class SupportTicketStatsSerializer(serializers.Serializer):
    """Serializer for support ticket statistics"""

    open_tickets = serializers.IntegerField()
    in_progress_tickets = serializers.IntegerField()
    tickets_today = serializers.IntegerField()
    avg_resolution_time = serializers.CharField()
    category_distribution = serializers.ListField(child=serializers.DictField())
    response_times = serializers.DictField()


class UserSupportTicketSerializer(serializers.ModelSerializer):
    """Support ticket serializer for user-facing views"""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    assigned_to_details = UserBriefSerializer(source="assigned_to", read_only=True)

    class Meta:
        model = SupportTicket
        fields = [
            "ticket_id",
            "subject",
            "description",
            "category",
            "category_display",
            "priority",
            "priority_display",
            "status",
            "status_display",
            "created_at",
            "updated_at",
            "assigned_to_details",
            "resolution_notes",
        ]
        read_only_fields = [
            "ticket_id",
            "status",
            "status_display",
            "created_at",
            "updated_at",
            "assigned_to_details",
        ]


class UserChatMessageSerializer(serializers.ModelSerializer):
    """Chat message serializer for user-facing views"""

    isUser = serializers.BooleanField(source="is_user")
    timestamp = serializers.DateTimeField(source="created_at")
    text = serializers.CharField(source="content")

    class Meta:
        model = ChatMessage
        fields = ["id", "text", "isUser", "timestamp"]


class UserSupportTicketDetailSerializer(serializers.ModelSerializer):
    """Detailed ticket serializer for user-facing view with conversation"""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    assigned_to_details = UserBriefSerializer(source="assigned_to", read_only=True)
    conversation = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = [
            "ticket_id",
            "subject",
            "description",
            "category",
            "category_display",
            "priority",
            "priority_display",
            "status",
            "status_display",
            "created_at",
            "updated_at",
            "assigned_to_details",
            "resolution_notes",
            "conversation",
        ]
        read_only_fields = [
            "ticket_id",
            "status",
            "status_display",
            "created_at",
            "updated_at",
            "assigned_to_details",
        ]

    def get_conversation(self, obj):
        """Get conversation messages if chat session exists"""
        if not obj.chat_session:
            return []

        messages = obj.chat_session.messages.all().order_by("created_at")
        return UserChatMessageSerializer(messages, many=True).data


class CreateSupportTicketSerializer(serializers.ModelSerializer):
    """Serializer for creating new support tickets"""

    class Meta:
        model = SupportTicket
        fields = ["subject", "description", "category", "priority"]

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["user"] = user

        # Use a transaction to ensure both Ticket and Session are created or neither are.
        with transaction.atomic():
            # First, create the chat session for this new ticket
            chat_session = ChatSession.objects.create(userId=user)

            # Add the ticket description as the first message in this new session
            ChatMessage.objects.create(
                session=chat_session,
                content=validated_data["description"],
                is_user=True,
                sender_type="user",  # Explicitly set sender type
            )

            # Now, create the ticket and link it to the session we just made
            validated_data["chat_session"] = chat_session
            ticket = SupportTicket.objects.create(**validated_data)

        return ticket


class ChatRequestSerializer(serializers.Serializer):
    messages = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField(), allow_empty=False),
        min_length=1,
    )
    session_id = serializers.IntegerField(required=False, allow_null=False)

    def validate(self, data):
        """
        Remove session_id if it's None to avoid validation errors
        """
        if "session_id" in data and data["session_id"] is None:
            data.pop("session_id")
        return data

    def validate_messages(self, value):
        """
        Validate that each message has the required fields and format
        """
        valid_roles = ["user", "assistant"]

        for message in value:
            if "role" not in message or "content" not in message:
                raise serializers.ValidationError(
                    "Each message must have 'role' and 'content' fields"
                )
            if message["role"] not in valid_roles:
                raise serializers.ValidationError(
                    f"Message role must be one of {valid_roles}"
                )

            # Check if it's a single emoji message (may appear empty after stripping HTML)
            emoji_pattern = r"^\s*:(\w+):\s*$"
            is_emoji_shortcode = bool(
                re.match(emoji_pattern, message["content"].strip())
            )

            # Only validate for non-empty content if it's not an emoji shortcode
            if not is_emoji_shortcode and not message["content"].strip():
                raise serializers.ValidationError("Message content cannot be empty")

        return value


class TicketCreationResponseSerializer(serializers.Serializer):
    """Serializer for including ticket information in chat responses"""

    message = ChatMessageSerializer()
    ticket = SupportTicketSerializer()
