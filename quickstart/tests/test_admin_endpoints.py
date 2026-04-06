"""Admin API smoke tests."""
import pytest
from rest_framework import status

from quickstart.tests.factories import UserFactory

API = "/api"


@pytest.mark.django_db
class TestAdminUsersList:
    def test_superuser_can_list_users(self, admin_client):
        r = admin_client.get(f"{API}/admin/users/")
        assert r.status_code == status.HTTP_200_OK

    def test_regular_user_forbidden(self, api_client):
        api_client.force_authenticate(user=UserFactory(is_staff=False, is_superuser=False))
        r = api_client.get(f"{API}/admin/users/")
        assert r.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED)
