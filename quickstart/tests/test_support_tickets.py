"""User support tickets API."""
from unittest.mock import patch

import pytest
from rest_framework import status

from quickstart.tests.factories import SupportTicketFactory, UserFactory

API = "/api"


@pytest.mark.django_db
class TestUserSupportTickets:
    def test_list_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/support-tickets/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED

    @patch("quickstart.views.public.user_support_views.send_support_ticket_created_email")
    def test_create_and_list_own_ticket(self, _mock_email, authenticated_client, user):
        r = authenticated_client.post(
            f"{API}/support-tickets/",
            {
                "subject": "Need help",
                "description": "Details here",
                "category": "other",
            },
            format="json",
        )
        assert r.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK)
        lst = authenticated_client.get(f"{API}/support-tickets/")
        assert lst.status_code == status.HTTP_200_OK

    def test_cannot_read_other_users_ticket(self, api_client):
        owner = UserFactory()
        other = UserFactory()
        t = SupportTicketFactory(user=owner, subject="Private")
        api_client.force_authenticate(user=other)
        r = api_client.get(f"{API}/support-tickets/{t.ticket_id}/")
        assert r.status_code == status.HTTP_404_NOT_FOUND
