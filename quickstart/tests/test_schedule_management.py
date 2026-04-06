"""Business schedule bulk actions."""
import pytest
from rest_framework import status

API = "/api"
BUSINESS = f"{API}/business"


@pytest.mark.django_db
class TestScheduleBulkCreate:
    def test_bulk_create_empty_body_400(self, business_owner_client):
        r = business_owner_client.post(
            f"{BUSINESS}/schedules/bulk-create/",
            {},
            format="json",
        )
        assert r.status_code == status.HTTP_400_BAD_REQUEST
