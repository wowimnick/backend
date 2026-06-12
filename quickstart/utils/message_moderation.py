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


def _apply_moderation_result(
    msg: ConversationMessage,
    *,
    new_status: str,
    reason: str,
    confidence: float | None,
    moderated_at,
) -> bool:
    """
    Write the moderation result to the DB using a conditional update that only
    applies when the message is still PENDING.  Returns True if the row was
    updated, False when another actor (admin override, concurrent retry) already
    changed the status — in which case the caller must not deliver the message.
    """
    updated = ConversationMessage.objects.filter(
        pk=msg.pk,
        moderation_status=ConversationMessage.MODERATION_PENDING,
    ).update(
        moderation_status=new_status,
        moderation_reason=reason[:2000],
        moderation_confidence=confidence,
        moderated_at=moderated_at,
    )
    return bool(updated)


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

    try:
        result = classify_message(
            msg.text,
            business_name=getattr(business, "businessName", None),
            sender_name=sender_name,
            sender_email=sender_email,
            message_id=message_id,
        )
        is_scam = bool(result.get("is_scam"))
        new_status = (
            ConversationMessage.MODERATION_REJECTED
            if is_scam
            else ConversationMessage.MODERATION_APPROVED
        )

        applied = _apply_moderation_result(
            msg,
            new_status=new_status,
            reason=result.get("reason") or "",
            confidence=result.get("confidence"),
            moderated_at=now,
        )

        if not applied:
            # An admin override arrived while classify_message was running.
            # The message has already been handled (and possibly delivered);
            # do NOT overwrite the admin decision or re-deliver.
            actual_status = (
                ConversationMessage.objects.filter(pk=msg.pk)
                .values_list("moderation_status", flat=True)
                .first()
            )
            logger.warning(
                "Moderation result for message_id=%s discarded: status changed to %r "
                "during classification (admin override or concurrent retry).",
                message_id,
                actual_status,
            )
            log_task_skipped(
                message_id=message_id,
                reason="superseded",
                moderation_status=actual_status,
            )
            return

        decision = "rejected" if is_scam else "approved"
        log_moderation_decision(
            message_id=message_id,
            conversation_id=conv.id,
            decision=decision,
            moderation_status=new_status,
            confidence=result.get("confidence"),
            reason=result.get("reason"),
        )
        if not is_scam:
            deliver_booker_message(conv, msg)

    except Exception as exc:
        log_task_failed_open(message_id=message_id, error=str(exc))
        logger.error(
            "message moderation failed for %s; fail-open deliver: %s",
            message_id,
            exc,
            exc_info=True,
        )

        applied = _apply_moderation_result(
            msg,
            new_status=ConversationMessage.MODERATION_ERROR,
            reason=str(exc),
            confidence=None,
            moderated_at=now,
        )

        if not applied:
            # Admin already resolved the message while the Gemini call was
            # failing; the message is already delivered — don't re-deliver.
            actual_status = (
                ConversationMessage.objects.filter(pk=msg.pk)
                .values_list("moderation_status", flat=True)
                .first()
            )
            logger.warning(
                "Fail-open delivery for message_id=%s skipped: status is already %r "
                "(admin resolved before error handler ran).",
                message_id,
                actual_status,
            )
            log_task_skipped(
                message_id=message_id,
                reason="superseded_on_error",
                moderation_status=actual_status,
            )
            return

        log_moderation_decision(
            message_id=message_id,
            conversation_id=conv.id,
            decision="fail_open",
            moderation_status=ConversationMessage.MODERATION_ERROR,
            reason=str(exc),
        )
        deliver_booker_message(conv, msg)
