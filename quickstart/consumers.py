"""
WebSocket consumer for conversation messaging: real-time messages, typing, read receipts.
Auth: JWT from cookie (booker or business) or guest_inbox_token in query string (guest).
"""

import logging
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken

from quickstart.models import (
    Conversation,
    ConversationMessage,
    BusinessInfo,
)
from quickstart.utils.guest_inbox_token import parse_guest_inbox_token

logger = logging.getLogger(__name__)

# Side identifiers for typing/read
SIDE_BOOKER = "booker"
SIDE_BUSINESS = "business"


def _get_cookie_from_scope(scope, name):
    """Extract cookie value from scope['headers']. Headers are list of (bytes, bytes)."""
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() == b"cookie":
            # Parse "key1=val1; key2=val2"
            for part in raw_value.decode("utf-8").split(";"):
                part = part.strip()
                if part.startswith(name + "="):
                    return part[len(name) + 1 :].strip()
    return None


def _get_user_from_jwt_token(token_string):
    """Validate JWT and return user or None. Runs in sync."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        access = AccessToken(token_string)
        user_id = access.get(settings.SIMPLE_JWT.get("USER_ID_CLAIM", "user_id"))
        return User.objects.get(pk=user_id)
    except (InvalidToken, KeyError, User.DoesNotExist):
        return None


def _resolve_conversation_auth_sync(conversation_id, cookie_token, query_token):
    """
    Resolve authenticated user/side and conversation. Returns dict:
    { "conversation": conv, "side": "booker"|"business", "user": user or None, "display_name": str }
    or { "error": str }.
    Runs in sync (call with sync_to_async).
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    User = get_user_model()
    conv = Conversation.objects.filter(id=conversation_id).select_related(
        "business", "booker_user", "booker_contact"
    ).first()
    if not conv:
        return {"error": "Conversation not found."}

    # 1) Guest inbox token (query)
    if query_token:
        parsed = parse_guest_inbox_token(query_token)
        if not parsed:
            return {"error": "Invalid or expired guest link."}
        cid, contact_id = parsed
        if str(conv.id) != cid or str(conv.booker_contact_id) != contact_id:
            return {"error": "Guest link does not match conversation."}
        if not conv.booker_contact_id:
            return {"error": "Not a guest conversation."}
        name = (
            f"{conv.booker_contact.first_name} {conv.booker_contact.last_name}".strip()
            or conv.booker_contact.email
            or "Guest"
        )
        return {
            "conversation": conv,
            "side": SIDE_BOOKER,
            "user": None,
            "display_name": name,
        }

    # 2) JWT from cookie
    token = cookie_token
    if not token:
        return {"error": "Authentication required."}
    user = _get_user_from_jwt_token(token)
    if not user:
        return {"error": "Invalid or expired session."}

    # Booker?
    if conv.booker_user_id == user.pk:
        name = user.get_full_name() or user.email or "Guest"
        return {
            "conversation": conv,
            "side": SIDE_BOOKER,
            "user": user,
            "display_name": name,
        }

    # Business?
    business = (
        BusinessInfo.objects.filter(
            Q(owner=user) | Q(staff_members__user=user)
        )
        .distinct()
        .first()
    )
    if business and conv.business_id == business.businessId:
        name = user.get_full_name() or user.email or "Host"
        return {
            "conversation": conv,
            "side": SIDE_BUSINESS,
            "user": user,
            "display_name": name,
        }

    return {"error": "You do not have access to this conversation."}


@database_sync_to_async
def resolve_conversation_auth(conversation_id, cookie_token, query_token):
    return _resolve_conversation_auth_sync(conversation_id, cookie_token, query_token)


class ConversationConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket at ws/conversations/<conversation_id>/
    Query: optional guest_inbox_token=... for guest auth.
    Cookie: JWT for logged-in booker or business.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conversation_id = None
        self.side = None
        self.display_name = None
        self.authenticated = False

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"].get("conversation_id")
        if not self.conversation_id:
            await self.close(code=4400)
            return

        cookie_name = settings.SIMPLE_JWT.get("AUTH_COOKIE", "my-app-auth")
        cookie_token = _get_cookie_from_scope(self.scope, cookie_name)
        query_string = (self.scope.get("query_string") or b"").decode("utf-8")
        query_params = parse_qs(query_string)
        query_token = (query_params.get("guest_inbox_token") or [None])[0]

        result = await resolve_conversation_auth(
            self.conversation_id, cookie_token, query_token
        )
        if "error" in result:
            await self.close(code=4401)
            return

        conv = result["conversation"]
        self.conversation_id = str(conv.id)
        self.side = result["side"]
        self.display_name = result["display_name"] or "User"
        self._user_pk = result["user"].pk if result.get("user") else None
        self._contact_id = conv.booker_contact_id if (result["side"] == SIDE_BOOKER and conv.booker_contact_id) else None
        self.authenticated = True

        self.room_group_name = f"conversation_{self.conversation_id}"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

        # Send joined confirmation
        await self.send_json(
            {
                "type": "joined",
                "conversation_id": self.conversation_id,
                "side": self.side,
                "display_name": self.display_name,
            }
        )

    async def disconnect(self, close_code):
        if self.room_group_name:
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def receive_json(self, content):
        if not self.authenticated:
            return
        action = content.get("action")
        if action == "typing_start":
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "conversation.typing",
                    "side": self.side,
                    "display_name": self.display_name,
                    "active": True,
                },
            )
        elif action == "typing_stop":
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "conversation.typing",
                    "side": self.side,
                    "display_name": self.display_name,
                    "active": False,
                },
            )
        elif action == "mark_read":
            await self._handle_mark_read()
        elif action == "send_message":
            text = (content.get("text") or "").strip()
            if text:
                await self._handle_send_message(text)

    async def _handle_mark_read(self):
        updated = await _mark_read_sync(self.conversation_id, self.side)
        if updated:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "conversation.read_receipt",
                    "side": self.side,
                    "read_at": updated,
                },
            )

    @database_sync_to_async
    def _create_message_and_broadcast_payload(self, text):
        """Create message in DB, trigger email, return payload for new_message. Runs in sync."""
        return _create_message_sync(
            self.conversation_id,
            self.side,
            self._user_pk,
            self._contact_id,
            self.display_name,
            text,
        )

    async def _handle_send_message(self, text):
        # Create message via sync; then broadcast new_message
        payload = await self._create_message_and_broadcast_payload(text)
        if payload:
            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "conversation.new_message", "message": payload},
            )

    # Group message handlers (type = conversation.xxx)
    async def conversation_typing(self, event):
        await self.send_json(
            {
                "type": "typing",
                "side": event["side"],
                "display_name": event["display_name"],
                "active": event["active"],
            }
        )

    async def conversation_read_receipt(self, event):
        await self.send_json(
            {
                "type": "read_receipt",
                "side": event["side"],
                "read_at": event["read_at"],
            }
        )

    async def conversation_new_message(self, event):
        await self.send_json({"type": "new_message", "message": event["message"]})


def _create_message_sync(conversation_id, side, user_pk, contact_id, display_name, text):
    """
    Create ConversationMessage, update last_message_at, send email.
    Returns JSON-serializable message payload or None.
    """
    from quickstart.utils.conversation_emails import (
        notify_booker_new_reply,
        notify_business_new_message,
    )

    conv = Conversation.objects.filter(id=conversation_id).first()
    if not conv:
        return None
    sender_type = (
        ConversationMessage.SENDER_BUSINESS
        if side == SIDE_BUSINESS
        else ConversationMessage.SENDER_BOOKER
    )
    sender_user_id = user_pk if (side == SIDE_BUSINESS or (side == SIDE_BOOKER and user_pk)) else None
    sender_contact_id = contact_id if (side == SIDE_BOOKER and contact_id) else None
    msg = ConversationMessage.objects.create(
        conversation=conv,
        sender_type=sender_type,
        sender_user_id=sender_user_id,
        sender_contact_id=sender_contact_id,
        text=text,
    )
    conv.last_message_at = timezone.now()
    conv.save(update_fields=["last_message_at"])
    if side == SIDE_BUSINESS:
        notify_booker_new_reply(conv, msg)
    else:
        notify_business_new_message(conv, msg)
        from quickstart.utils.notification_utils import create_notifications_for_new_chat_message
        create_notifications_for_new_chat_message(conv, msg)
    return {
        "id": str(msg.id),
        "conversation": str(msg.conversation_id),
        "sender_type": msg.sender_type,
        "sender_user": msg.sender_user_id,
        "sender_contact": msg.sender_contact_id,
        "sender_display": display_name or "Unknown",
        "text": msg.text,
        "created_at": msg.created_at.isoformat(),
    }


@database_sync_to_async
def _mark_read_sync(conversation_id, side):
    """Update last_read_by_* and return ISO read_at or None."""
    conv = Conversation.objects.filter(id=conversation_id).first()
    if not conv:
        return None
    now = timezone.now()
    if side == SIDE_BOOKER:
        conv.last_read_by_booker_at = now
        conv.save(update_fields=["last_read_by_booker_at"])
        return now.isoformat()
    if side == SIDE_BUSINESS:
        conv.last_read_by_business_at = now
        conv.save(update_fields=["last_read_by_business_at"])
        return now.isoformat()
    return None


# ---------------------------------------------------------------------------
# Notifications WebSocket (real-time bell updates)
# ---------------------------------------------------------------------------


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket at api/ws/notifications/. Auth: JWT cookie only.
    User joins group notifications_user_<user_id> and receives new_notification events.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_id = None
        self.room_group_name = None

    async def connect(self):
        cookie_name = settings.SIMPLE_JWT.get("AUTH_COOKIE", "my-app-auth")
        token = _get_cookie_from_scope(self.scope, cookie_name)
        if not token:
            await self.close(code=4401)
            return
        user = await database_sync_to_async(_get_user_from_jwt_token)(token)
        if not user:
            await self.close(code=4401)
            return
        self.user_id = user.pk
        self.room_group_name = f"notifications_user_{self.user_id}"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        await self.send_json({"type": "joined", "user_id": self.user_id})

    async def disconnect(self, close_code):
        if self.room_group_name:
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )

    async def notification_new(self, event):
        await self.send_json({
            "type": "new_notification",
            "notification": event.get("notification"),
            "unread_delta": event.get("unread_delta", 1),
        })
