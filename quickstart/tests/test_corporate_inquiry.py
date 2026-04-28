"""
Tests for public corporate inquiry API and email task recipients.
"""
from unittest.mock import MagicMock, patch

import pytest

from quickstart.models import CorporateInquiry
from quickstart.tasks.corporate_tasks import send_corporate_inquiry_emails
from quickstart.tests.factories import RoleFactory, UserFactory
from quickstart.views.public.corporate_views import (
    CorporateInquiryCreateThrottle,
    CorporateInquiryCreateView,
)

API = "/api"


@pytest.mark.django_db
class TestCorporateInquiryCreateView:
    def test_post_valid_returns_201(self, api_client):
        payload = {
            "company_name": "Acme Corp",
            "contact_name": "Jane Doe",
            "email": "jane@acme.example",
            "phone": "",
            "company_size": "11-50",
            "message": "Planning a team offsite.",
            "meta": {"source": "corporate_page", "city": "NYC"},
        }
        with patch(
            "quickstart.tasks.email_tasks.send_transactional_email_task.delay"
        ) as mock_delay:
            response = api_client.post(
                f"{API}/corporate-inquiry/",
                data=payload,
                format="json",
            )
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        # Confirmation always queued; internal only if super-admins / CORPORATE_LEADS_EMAIL exist.
        assert mock_delay.call_count >= 1
        assert CorporateInquiry.objects.filter(pk=data["id"]).exists()

    def test_post_invalid_email_returns_400(self, api_client):
        payload = {
            "company_name": "Acme Corp",
            "contact_name": "Jane Doe",
            "email": "not-an-email",
            "message": "",
        }
        with patch(
            "quickstart.tasks.email_tasks.send_transactional_email_task.delay",
        ):
            response = api_client.post(
                f"{API}/corporate-inquiry/",
                data=payload,
                format="json",
            )
        assert response.status_code == 400

    def test_anon_throttle_wired_to_view(self):
        """DRF anonymous clients are throttled at 12/hour (see CorporateInquiryCreateThrottle)."""
        assert CorporateInquiryCreateThrottle in CorporateInquiryCreateView.throttle_classes
        assert CorporateInquiryCreateThrottle.rate == "12/hour"


@pytest.mark.django_db
class TestSendCorporateInquiryEmailsRecipients:
    def test_internal_email_recipients_include_super_admins(self):
        super_role = RoleFactory(name="Super Admin")
        UserFactory(
            email="superadmin-corporate-test@example.com",
            role=super_role,
        )

        inquiry = CorporateInquiry.objects.create(
            company_name="Widget Inc",
            contact_name="Alex",
            email="alex@widget.example",
            phone="",
            company_size="1-10",
            message="Hello",
            meta={"source": "test"},
        )

        queued = []

        def capture_delay(**kwargs):
            queued.append(kwargs)
            m = MagicMock()
            m.id = f"tid-{len(queued)}"
            return m

        with patch(
            "quickstart.tasks.email_tasks.send_transactional_email_task.delay",
            side_effect=capture_delay,
        ):
            send_corporate_inquiry_emails(str(inquiry.id))

        assert len(queued) == 2
        internal_recipients = queued[0]["to"]
        assert "superadmin-corporate-test@example.com" in internal_recipients
