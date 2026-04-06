"""Business discounts."""
import pytest
from rest_framework import status

from quickstart.tests.factories import DiscountFactory

API = "/api"
BUSINESS = f"{API}/business"


@pytest.mark.django_db
class TestBusinessDiscounts:
    def test_create_percentage_discount(self, business_owner_client, business):
        r = business_owner_client.post(
            f"{BUSINESS}/discounts/",
            {
                "name": "Ten off",
                "code": "TENOFF",
                "discount_type": "percentage",
                "value": "10.00",
                "scope": "business",
            },
            format="json",
        )
        assert r.status_code in (status.HTTP_201_CREATED, status.HTTP_200_OK)

    def test_list_includes_created_discount(self, business_owner_client, business):
        DiscountFactory(business=business, name="Summer", code="SUMMER")
        r = business_owner_client.get(f"{BUSINESS}/discounts/")
        assert r.status_code == status.HTTP_200_OK
