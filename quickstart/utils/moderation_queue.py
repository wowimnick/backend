"""Queue a booker message for async Gemini scam moderation."""

from __future__ import annotations

from django.db import transaction

from quickstart.utils.scam_moderation_log import log_task_queued


def queue_message_moderation(message, *, source: str) -> None:
    """Enqueue Celery moderation after the DB transaction commits."""

    def _enqueue():
        from quickstart.tasks.business_tasks import moderate_message_task

        async_result = moderate_message_task.delay(str(message.id))
        log_task_queued(
            message_id=message.id,
            celery_task_id=getattr(async_result, "id", None),
            source=source,
        )

    transaction.on_commit(_enqueue)
