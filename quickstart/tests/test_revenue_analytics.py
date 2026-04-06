"""Revenue analytics API."""
import pytest
from rest_framework import status

API = "/api"


@pytest.mark.django_db
class TestRevenueAnalytics:
    def test_unauthenticated_401(self, api_client):
        r = api_client.get(f"{API}/revenue/analytics/")
        assert r.status_code == status.HTTP_401_UNAUTHORIZED

    def test_owner_get_200(self, business_owner_client):
        r = business_owner_client.get(
            f"{API}/revenue/analytics/",
            {"start_date": "2020-01-01", "end_date": "2020-01-31"},
        )
        assert r.status_code == status.HTTP_200_OK
        data = r.json()
        assert isinstance(data, dict)
