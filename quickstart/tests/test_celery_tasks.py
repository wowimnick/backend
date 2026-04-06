"""Celery tasks callable smoke tests (run synchronously)."""
import pytest

from quickstart.tasks.booking_tasks import release_expired_spots, send_upcoming_booking_reminders
from quickstart.tasks.email_marketing_tasks import dispatch_due_scheduled_marketing_campaigns
from quickstart.tasks.email_marketing_workflow_tasks import process_due_workflow_enrollments
from quickstart.tasks.payout_tasks import update_completed_booking_status
from quickstart.tasks.user_tasks import send_pending_review_requests


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
