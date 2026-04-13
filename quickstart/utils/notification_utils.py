"""
Helpers for creating in-app notifications (Notification model) and broadcasting over WebSocket.
"""

import logging

from quickstart.models import Notification

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


def create_notification_for_recipients(
    business,
    notification_type,
    message,
    icon,
    color,
    link_web,
    content_type=None,
    object_id=None,
    recipients=None,
):
    """
    Create the same in-app notification for business owner + accepted staff (or a custom recipient list).
    """
    from quickstart.signals import _increment_user_unread_count

    if recipients is None:
        recipients = _get_business_notification_recipients(business)
    for user in recipients:
        if not user:
            continue
        try:
            Notification.objects.create(
                user=user,
                business=business,
                notification_type=notification_type,
                message=message,
                content_type=content_type,
                object_id=object_id,
                icon=icon,
                color=color,
                link_web=link_web,
            )
            _increment_user_unread_count(user.pk)
        except Exception as e:
            logger.warning(
                "Failed to create notification %s for user %s: %s",
                notification_type,
                getattr(user, "email", user.pk),
                e,
            )


def create_notifications_for_users(
    users,
    notification_type,
    message,
    icon,
    color,
    link_web,
    business=None,
    content_type=None,
    object_id=None,
):
    """Create an in-app notification for each user in ``users`` (deduplicated by pk)."""
    from quickstart.signals import _increment_user_unread_count

    seen = set()
    for user in users:
        if not user or not getattr(user, "pk", None) or user.pk in seen:
            continue
        seen.add(user.pk)
        try:
            Notification.objects.create(
                user=user,
                business=business,
                notification_type=notification_type,
                message=message,
                content_type=content_type,
                object_id=object_id,
                icon=icon,
                color=color,
                link_web=link_web,
            )
            _increment_user_unread_count(user.pk)
        except Exception as e:
            logger.warning(
                "Failed to create notification %s for user %s: %s",
                notification_type,
                getattr(user, "email", user.pk),
                e,
            )


def membership_member_display_name(membership):
    """Human-readable member name for membership lifecycle messages."""
    u = getattr(membership, "user", None)
    if u:
        return (u.get_full_name() or "").strip() or (getattr(u, "email", None) or "A member")
    c = getattr(membership, "contact", None)
    if c:
        name = f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
        return name or (getattr(c, "email", None) or "A member")
    return "A member"


def create_membership_business_in_app_notifications(membership, lifecycle_event):
    """
    In-app notifications for business team when customer membership lifecycle changes.

    lifecycle_event: 'activated' | 'canceled' | 'renewed' | 'payment_failed'
    """
    if not membership:
        return
    try:
        product = membership.product
        business = product.business
    except Exception:
        return

    member_name = membership_member_display_name(membership)
    prod_name = getattr(product, "name", None) or "membership"

    if lifecycle_event == "activated":
        ntype = "membership_new"
        msg = f"{member_name} joined your '{prod_name}' membership."
    elif lifecycle_event == "canceled":
        ntype = "membership_cancelled"
        msg = f"{member_name} cancelled your '{prod_name}' membership."
    elif lifecycle_event == "renewed":
        ntype = "membership_renewed"
        msg = f"{member_name} renewed your '{prod_name}' membership."
    elif lifecycle_event == "payment_failed":
        ntype = "payment_failed"
        msg = f"A renewal payment failed for {member_name} on '{prod_name}'."
    else:
        return

    create_notification_for_recipients(
        business,
        ntype,
        msg,
        "CreditCard",
        "#6366f1",
        "/business/dashboard/memberships",
        content_type=None,
        object_id=str(membership.pk),
    )


def create_notifications_for_new_chat_message(conversation, message):
    """
    When a booker (or guest) sends a message to the business, create in-app notifications
    for the business owner and accepted staff. Call from REST send_message and from WS consumer.
    """
    from quickstart.models import ConversationMessage

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
    msg_display = f'{sender_name} sent a message: "{message_text}"'
    link_web = f"/business/dashboard/messages?conversationId={conversation.id}"

    create_notification_for_recipients(
        business,
        "new_message_chat",
        msg_display,
        "MessageSquare",
        "#ff385c",
        link_web,
        recipients=recipients,
    )
    logger.info(
        "New chat notifications created for business %s (%s recipients)",
        getattr(business, "businessId", business.pk),
        len(recipients),
    )


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
