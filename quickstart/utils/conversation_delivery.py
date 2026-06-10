"""Side-effects when a booker message is delivered to the business."""


def deliver_booker_message(conversation, message):
    """Notify business (email, in-app, WebSocket) for an approved booker message."""
    from quickstart.utils.conversation_emails import notify_business_new_message
    from quickstart.utils.conversation_ws_broadcast import broadcast_new_message
    from quickstart.utils.notification_utils import create_notifications_for_new_chat_message

    notify_business_new_message(conversation, message)
    create_notifications_for_new_chat_message(conversation, message)
    broadcast_new_message(message)
