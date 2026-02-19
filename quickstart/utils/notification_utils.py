"""
Helpers for creating in-app notifications (Notification model) and broadcasting over WebSocket.
"""

import logging

from quickstart.models import Notification, BusinessInfo

logger = logging.getLogger(__name__)


def _get_business_notification_recipients(business):
    """
    Return list of users to notify for a business event: owner + staff with status=accepted.
    Each item is a User instance.
    """
    if not business:
        return []
    recipients = []
    if business.owner_id:
        recipients.append(business.owner)
    for staff in business.staff_members.filter(status="accepted", user__isnull=False).select_related("user"):
        if staff.user_id and staff.user_id != business.owner_id:
            recipients.append(staff.user)
    return recipients


def create_notifications_for_new_chat_message(conversation, message):
    """
    When a booker (or guest) sends a message to the business, create in-app notifications
    for the business owner and accepted staff. Call from REST send_message and from WS consumer.
    """
    from quickstart.models import ConversationMessage
    from quickstart.signals import _increment_user_unread_count

    if not message or message.sender_type != ConversationMessage.SENDER_BOOKER:
        return
    business = getattr(conversation, "business", None)
    if not business:
        return
    recipients = _get_business_notification_recipients(business)
    sender_name = "A guest"
    if message.sender_user:
        sender_name = message.sender_user.get_full_name() or message.sender_user.email or "A guest"
    elif message.sender_contact:
        sender_name = (
            f"{message.sender_contact.first_name} {message.sender_contact.last_name}".strip()
            or message.sender_contact.email
            or "A guest"
        )
    message_text = (message.text or "").strip()[:80]
    if len((message.text or "").strip()) > 80:
        message_text += "…"
    msg_display = f"{sender_name} sent a message: \"{message_text}\""
    link_web = f"/business/dashboard?tab=messages&conversation_id={conversation.id}"

    for user in recipients:
        if not user:
            continue
        try:
            Notification.objects.create(
                user=user,
                business=business,
                notification_type="new_message_chat",
                message=msg_display,
                icon="MessageSquare",
                color="#ff385c",
                link_web=link_web,
            )
            _increment_user_unread_count(user.pk)
            logger.info("New chat notification created for user %s", user.email)
        except Exception as e:
            logger.warning("Failed to create chat notification for user %s: %s", getattr(user, "email", user.pk), e)


def _notification_payload(notification):
    """Build JSON-serializable payload for WS push (matches NotificationSerializer shape)."""
    from django.utils.timesince import timesince
    return {
        "id": str(notification.id),
        "user": notification.user_id,
        "business": notification.business_id,
        "message": notification.message,
        "notification_type": notification.notification_type,
        "notification_type_display": notification.get_notification_type_display(),
        "is_read": notification.is_read,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
        "time_since": f"{timesince(notification.created_at).split(',')[0]} ago" if notification.created_at else "",
        "icon": notification.icon,
        "color": notification.color,
        "link_web": notification.link_web,
    }


def broadcast_notification_to_user(notification):
    """
    Push a new notification to the user's WebSocket channel (for real-time bell updates).
    Call from post_save signal or after creating a Notification.
    """
    if not notification or not notification.user_id:
        return
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    group_name = f"notifications_user_{notification.user_id}"
    payload = _notification_payload(notification)
    try:
        async_to_sync(channel_layer.group_send)(
            group_name,
            {"type": "notification.new", "notification": payload, "unread_delta": 1},
        )
    except Exception as e:
        logger.warning("Failed to broadcast notification to WS: %s", e)
