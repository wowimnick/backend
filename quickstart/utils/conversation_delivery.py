"""Side-effects when a booker message is delivered to the business."""

from quickstart.utils.scam_moderation_log import log_delivery


def deliver_booker_message(conversation, message):
    """Notify business (email, in-app, WebSocket) for an approved booker message."""
    from quickstart.utils.conversation_emails import notify_business_new_message
    from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
    from quickstart.utils.notification_utils import create_notifications_for_new_chat_message

    business_id = getattr(conversation, "business_id", None)
    log_delivery(
        message_id=message.id,
        conversation_id=conversation.id,
        business_id=business_id,
        stage="start",
    )
    notify_business_new_message(conversation, message)
    create_notifications_for_new_chat_message(conversation, message)
    broadcast_new_message(message)
    log_delivery(
        message_id=message.id,
        conversation_id=conversation.id,
        business_id=business_id,
        stage="complete",
    )
