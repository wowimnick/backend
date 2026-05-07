"""Celery tasks callable smoke tests (run synchronously)."""
import pytest
from django.conf import settings

from quickstart.tasks.booking_tasks import release_expired_spots, send_upcoming_booking_reminders
from quickstart.tasks.email_marketing_tasks import dispatch_due_scheduled_marketing_campaigns
from quickstart.tasks.email_marketing_workflow_tasks import process_due_workflow_enrollments
from quickstart.tasks.payout_tasks import update_completed_booking_status
from quickstart.tasks.user_tasks import send_pending_review_requests


@pytest.mark.django_db
def test_beat_and_critical_tasks_are_registered_on_celery_app():
    """Regression: worker KeyError / NotRegistered when deploy skew omits task modules."""
    import quickstart.tasks  # noqa: F401 — same registration path as Celery worker_ready

    from CEBackend.celery import (
        _EXTRA_REQUIRED_CELERY_TASKS,
        app,
        _required_celery_task_names,
    )

    required = set(_required_celery_task_names())
    assert _EXTRA_REQUIRED_CELERY_TASKS <= required
    beat = getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}
    assert beat, "CELERY_BEAT_SCHEDULE should define scheduled tasks"
    missing = [name for name in sorted(required) if name not in app.tasks]
    assert not missing, f"Tasks missing from Celery registry: {missing}"


@pytest.mark.django_db
def test_release_expired_spots_runs():
    release_expired_spots()


@pytest.mark.django_db
def test_send_upcoming_booking_reminders_runs():
    send_upcoming_booking_reminders()


@pytest.mark.django_db
def test_update_completed_booking_status_runs():
    update_completed_booking_status()


@pytest.mark.django_db
def test_dispatch_due_scheduled_marketing_campaigns_runs():
    dispatch_due_scheduled_marketing_campaigns()


@pytest.mark.django_db
def test_process_due_workflow_enrollments_runs():
    process_due_workflow_enrollments()


@pytest.mark.django_db
def test_send_pending_review_requests_runs():
    send_pending_review_requests()
