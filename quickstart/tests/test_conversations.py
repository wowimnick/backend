"""Guest and business messaging APIs."""
import pytest
from rest_framework import status


API = "/api"


@pytest.mark.django_db
class TestGuestConversations:
    def test_list_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/conversations/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED

    def test_authenticated_user_list_200(self, authenticated_client):
        r = authenticated_client.get(f"{API}/conversations/")
        assert r.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestBusinessConversations:
    def test_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/business/conversations/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.django_db
class TestGuestInbox:
    def test_missing_token_401_or_400(self, api_client):
        r = api_client.get(f"{API}/guest-inbox/")
        assert r.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_403_FORBIDDEN,
        )
