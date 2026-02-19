"""
Broadcast conversation events to WebSocket clients (e.g. when message is sent via REST).
"""

import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _message_payload(msg):
    """Build JSON-serializable message payload for new_message event."""
    from quickstart.serializers.public.public_conversation_serializers import (
        ConversationMessageSerializer,
    )
    data = ConversationMessageSerializer(msg).data
    # Normalize for JSON (UUID and datetime to str)
    return {
        "id": str(data["id"]),
        "conversation": str(data["conversation"]),
        "sender_type": data["sender_type"],
        "sender_user": data["sender_user"],
        "sender_contact": data["sender_contact"],
        "sender_display": data["sender_display"],
        "text": data["text"],
        "created_at": data["created_at"] if isinstance(data["created_at"], str) else data["created_at"].isoformat(),
    }


def broadcast_new_message(msg):
    """
    After creating a ConversationMessage (e.g. in REST send_message), broadcast it
    to all WebSocket clients in this conversation's group.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    group_name = f"conversation_{msg.conversation_id}"
    payload = _message_payload(msg)
    try:
        async_to_sync(channel_layer.group_send)(
            group_name,
            {"type": "conversation.new_message", "message": payload},
        )
    except Exception as e:
        logger.warning("Failed to broadcast new_message to WS: %s", e)
