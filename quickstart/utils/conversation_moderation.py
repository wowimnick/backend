"""Query helpers for message moderation visibility."""

from quickstart.models import Conversation, ConversationMessage

BUSINESS_VISIBLE_MODERATION_STATUSES = (
    ConversationMessage.MODERATION_APPROVED,
    ConversationMessage.MODERATION_ERROR,
)


def business_visible_messages_queryset():
    """Messages visible to business dashboards (excludes pending/rejected)."""
    return ConversationMessage.objects.filter(
        moderation_status__in=BUSINESS_VISIBLE_MODERATION_STATUSES
    )


def business_visible_conversations_queryset():
    """Conversations with at least one approved booker message."""
    return Conversation.objects.filter(
        messages__sender_type=ConversationMessage.SENDER_BOOKER,
        messages__moderation_status__in=BUSINESS_VISIBLE_MODERATION_STATUSES,
    ).distinct()
