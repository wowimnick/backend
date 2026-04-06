"""Membership products and widget membership endpoints."""
import pytest
from django.contrib.auth.models import Permission
from rest_framework import status

from quickstart.tests.factories import MembershipProductFactory

API = "/api"


def _grant_manage_own_classes(user):
    perm = Permission.objects.filter(
        codename="manage_own_classes",
        content_type__app_label="quickstart",
    ).first()
    if perm:
        user.user_permissions.add(perm)


@pytest.mark.django_db
class TestMyBusinessMembershipProducts:
    def test_list_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/my-business/membership-products/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED

    def test_list_owner_200(self, business_owner_client, business):
        _grant_manage_own_classes(business.owner)
        MembershipProductFactory(business=business, name="Gold")
        r = business_owner_client.get(f"{API}/my-business/membership-products/")
        assert r.status_code == status.HTTP_200_OK
        data = r.json()
        assert isinstance(data, list)
        assert any(p.get("name") == "Gold" for p in data)


@pytest.mark.django_db
class TestWidgetMembershipProducts:
    def test_requires_business_header(self, api_client):
        r = api_client.get(f"{API}/widget/v1/membership-products/")
        assert r.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

    def test_with_valid_key_200(self, api_client, business):
        MembershipProductFactory(business=business, is_active=True)
        r = api_client.get(
            f"{API}/widget/v1/membership-products/",
            HTTP_X_BUSINESS_ID=str(business.widget_api_key),
        )
        assert r.status_code == status.HTTP_200_OK
