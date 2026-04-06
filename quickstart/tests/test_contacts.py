"""Business CRM contacts API."""
import pytest
from django.contrib.auth.models import Permission
from rest_framework import status

from quickstart.tests.factories import ContactFactory

API = "/api"


def _grant_dashboard_access(user):
    perm = Permission.objects.filter(
        codename="access_business_dashboard",
        content_type__app_label="quickstart",
    ).first()
    if perm:
        user.user_permissions.add(perm)


@pytest.mark.django_db
class TestContacts:
    def test_list_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/my-business/contacts/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_sees_contacts(self, business_owner_client, business):
        _grant_dashboard_access(business.owner)
        ContactFactory(business=business, first_name="Alice")
        r = business_owner_client.get(f"{API}/my-business/contacts/")
        assert r.status_code == status.HTTP_200_OK
        results = r.json().get("results", [])
        assert any(c.get("first_name") == "Alice" for c in results)

    def test_other_business_contact_not_in_list(
        self, business_owner_client, business, other_business
    ):
        _grant_dashboard_access(business.owner)
        ContactFactory(business=other_business, first_name="Intruder")
        r = business_owner_client.get(f"{API}/my-business/contacts/")
        assert r.status_code == status.HTTP_200_OK
        assert b"Intruder" not in r.content
