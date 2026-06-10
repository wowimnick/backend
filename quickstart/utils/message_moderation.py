"""Shared scam-moderation runner for Celery task and worker fallbacks."""

from __future__ import annotations

import logging

from django.utils import timezone

from quickstart.models import ConversationMessage
from quickstart.utils.conversation_delivery import deliver_booker_message
from quickstart.utils.scam_filter_ai import classify_message
from quickstart.utils.scam_moderation_log import (
    log_moderation_decision,
    log_task_failed_open,
    log_task_skipped,
    log_task_started,
)

logger = logging.getLogger(__name__)

MODERATE_MESSAGE_TASK_NAME = "quickstart.tasks.business_tasks.moderate_message_task"


def run_message_moderation(
    message_id: str,
    *,
    celery_task_id: str | None = None,
    attempt: int = 1,
) -> None:
    """
    Run Gemini scam moderation for a quarantined booker message.
    Fail-open on errors so genuine leads are never lost.
    """
    log_task_started(
        message_id=message_id,
        celery_task_id=celery_task_id,
        attempt=attempt,
    )

    try:
        msg = (
            ConversationMessage.objects.select_related(
                "conversation",
                "conversation__business",
                "sender_user",
                "sender_contact",
            )
            .get(pk=message_id)
        )
    except ConversationMessage.DoesNotExist:
        log_task_skipped(message_id=message_id, reason="message_not_found")
        return

    if msg.moderation_status != ConversationMessage.MODERATION_PENDING:
        log_task_skipped(
            message_id=message_id,
            reason="not_pending",
            moderation_status=msg.moderation_status,
        )
        return

    conv = msg.conversation
    business = conv.business
    sender_name = "Guest"
    sender_email = None
    if msg.sender_user:
        sender_name = msg.sender_user.get_full_name() or msg.sender_user.email or sender_name
        sender_email = msg.sender_user.email
    elif msg.sender_contact:
        sender_name = (
            f"{msg.sender_contact.first_name} {msg.sender_contact.last_name}".strip()
            or msg.sender_contact.email
            or sender_name
        )
        sender_email = msg.sender_contact.email

    now = timezone.now()
    update_fields = [
        "moderation_status",
        "moderation_reason",
        "moderation_confidence",
        "moderated_at",
    ]

    try:
        result = classify_message(
            msg.text,
            business_name=getattr(business, "businessName", None),
            sender_name=sender_name,
            sender_email=sender_email,
            message_id=message_id,
        )
        if result.get("is_scam"):
            msg.moderation_status = ConversationMessage.MODERATION_REJECTED
            msg.moderation_reason = (result.get("reason") or "")[:2000]
            msg.moderation_confidence = result.get("confidence")
            msg.moderated_at = now
            msg.save(update_fields=update_fields)
            log_moderation_decision(
                message_id=message_id,
                conversation_id=conv.id,
                decision="rejected",
                moderation_status=msg.moderation_status,
                confidence=result.get("confidence"),
                reason=result.get("reason"),
            )
            return

        msg.moderation_status = ConversationMessage.MODERATION_APPROVED
        msg.moderation_reason = (result.get("reason") or "")[:2000]
        msg.moderation_confidence = result.get("confidence")
        msg.moderated_at = now
        msg.save(update_fields=update_fields)
        log_moderation_decision(
            message_id=message_id,
            conversation_id=conv.id,
            decision="approved",
            moderation_status=msg.moderation_status,
            confidence=result.get("confidence"),
            reason=result.get("reason"),
        )
        deliver_booker_message(conv, msg)
    except Exception as exc:
        log_task_failed_open(message_id=message_id, error=str(exc))
        logger.error(
            "message moderation failed for %s; fail-open deliver: %s",
            message_id,
            exc,
            exc_info=True,
        )
        msg.moderation_status = ConversationMessage.MODERATION_ERROR
        msg.moderation_reason = str(exc)[:2000]
        msg.moderated_at = now
        msg.save(update_fields=update_fields)
        log_moderation_decision(
            message_id=message_id,
            conversation_id=conv.id,
            decision="fail_open",
            moderation_status=msg.moderation_status,
            reason=str(exc),
        )
        deliver_booker_message(conv, msg)
