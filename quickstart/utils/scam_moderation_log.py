"""Structured logging for booker message scam moderation (Gemini queue + delivery)."""

from __future__ import annotations

import logging

logger = logging.getLogger("quickstart.scam_moderation")


def _fmt(**fields) -> str:
    return " ".join(f"{key}={value!r}" for key, value in fields.items() if value is not None)


def log_message_quarantined(
    *,
    message_id,
    conversation_id,
    business_id,
    sender_type: str,
    source: str,
    scam_filter_enabled: bool,
    is_first_booker_message: bool | None = None,
    sender_ip: str | None = None,
):
    logger.info(
        "scam_moderation quarantined %s",
        _fmt(
            message_id=str(message_id),
            conversation_id=str(conversation_id),
            business_id=str(business_id) if business_id else None,
            sender_type=sender_type,
            source=source,
            scam_filter_enabled=scam_filter_enabled,
            is_first_booker_message=is_first_booker_message,
            sender_ip=sender_ip,
        ),
    )


def log_message_delivered_immediately(
    *,
    message_id,
    conversation_id,
    source: str,
    reason: str,
):
    logger.info(
        "scam_moderation immediate_delivery %s",
        _fmt(
            message_id=str(message_id),
            conversation_id=str(conversation_id),
            source=source,
            reason=reason,
        ),
    )


def log_task_queued(*, message_id, celery_task_id: str | None, source: str):
    logger.info(
        "scam_moderation task_queued %s",
        _fmt(
            message_id=str(message_id),
            celery_task_id=celery_task_id,
            source=source,
        ),
    )


def log_task_started(*, message_id, celery_task_id: str | None, attempt: int):
    logger.info(
        "scam_moderation task_started %s",
        _fmt(
            message_id=str(message_id),
            celery_task_id=celery_task_id,
            attempt=attempt,
        ),
    )


def log_task_skipped(*, message_id, reason: str, moderation_status: str | None = None):
    logger.info(
        "scam_moderation task_skipped %s",
        _fmt(
            message_id=str(message_id),
            reason=reason,
            moderation_status=moderation_status,
        ),
    )


def log_gemini_request(
    *,
    message_id,
    model: str,
    business_name: str | None,
    sender_name: str | None,
    text_preview: str,
):
    logger.info(
        "scam_moderation gemini_request %s",
        _fmt(
            message_id=str(message_id),
            model=model,
            business_name=business_name,
            sender_name=sender_name,
            text_preview=text_preview[:120],
        ),
    )


def log_gemini_result(
    *,
    message_id,
    model: str,
    is_scam: bool,
    confidence: float | None,
    reason: str | None,
):
    logger.info(
        "scam_moderation gemini_result %s",
        _fmt(
            message_id=str(message_id),
            model=model,
            is_scam=is_scam,
            confidence=confidence,
            reason=(reason or "")[:200] or None,
        ),
    )


def log_gemini_model_failed(*, message_id, model: str, status_code, retryable: bool, error: str):
    logger.warning(
        "scam_moderation gemini_model_failed %s",
        _fmt(
            message_id=str(message_id),
            model=model,
            status_code=status_code,
            retryable=retryable,
            error=error[:300],
        ),
    )


def log_moderation_decision(
    *,
    message_id,
    conversation_id,
    decision: str,
    moderation_status: str,
    confidence: float | None = None,
    reason: str | None = None,
):
    logger.info(
        "scam_moderation decision %s",
        _fmt(
            message_id=str(message_id),
            conversation_id=str(conversation_id),
            decision=decision,
            moderation_status=moderation_status,
            confidence=confidence,
            reason=(reason or "")[:200] or None,
        ),
    )


def log_delivery(*, message_id, conversation_id, business_id, stage: str):
    logger.info(
        "scam_moderation delivery %s",
        _fmt(
            message_id=str(message_id),
            conversation_id=str(conversation_id),
            business_id=str(business_id) if business_id else None,
            stage=stage,
        ),
    )


def log_task_failed_open(*, message_id, error: str):
    logger.error(
        "scam_moderation fail_open %s",
        _fmt(message_id=str(message_id), error=error[:500]),
    )


def log_admin_override(
    *,
    message_id,
    conversation_id,
    action: str,
    admin_user_id,
    ban_ip: bool = False,
):
    logger.info(
        "scam_moderation admin_override %s",
        _fmt(
            message_id=str(message_id),
            conversation_id=str(conversation_id),
            action=action,
            admin_user_id=admin_user_id,
            ban_ip=ban_ip,
        ),
    )
