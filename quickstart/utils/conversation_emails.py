"""
Guest–business messaging email notifications with cooldown to avoid spam.
Implementations call send_guest_message_notification_email and send_business_reply_notification_email.
"""


def notify_business_new_message(conversation, message):
    """Called when a booker sends a message. Notify business (owner + staff with permission)."""
    from quickstart.utils.email_utils import send_guest_message_notification_email
    send_guest_message_notification_email(conversation, message)


def notify_booker_new_reply(conversation, message):
    """Called when business sends a reply. Notify booker (user or contact email)."""
    from quickstart.utils.email_utils import send_business_reply_notification_email
    send_business_reply_notification_email(conversation, message)
