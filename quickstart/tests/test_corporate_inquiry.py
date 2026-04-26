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
            "quickstart.views.public.corporate_views.send_corporate_inquiry_emails.delay"
        ) as mock_delay:
            response = api_client.post(
                f"{API}/corporate-inquiry/",
                data=payload,
                format="json",
            )
        assert response.status_code == 201
        data = response.json()
        assert "id" in data
        mock_delay.assert_called_once()
        assert CorporateInquiry.objects.filter(pk=data["id"]).exists()

    def test_post_invalid_email_returns_400(self, api_client):
        payload = {
            "company_name": "Acme Corp",
            "contact_name": "Jane Doe",
            "email": "not-an-email",
            "message": "",
        }
        with patch(
            "quickstart.views.public.corporate_views.send_corporate_inquiry_emails.delay"
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

        sent_batches = []

        def make_msg(*args, **kwargs):
            to = kwargs.get("to")
            if to is None and len(args) > 3:
                to = args[3]
            m = MagicMock()
            m.send = lambda fail_silently=False: sent_batches.append(list(to or []))
            m.attach_alternative = MagicMock()
            return m

        with patch(
            "quickstart.tasks.corporate_tasks.EmailMultiAlternatives",
            side_effect=make_msg,
        ):
            send_corporate_inquiry_emails(str(inquiry.id))

        assert len(sent_batches) >= 2
        internal_recipients = sent_batches[0]
        assert "superadmin-corporate-test@example.com" in internal_recipients
