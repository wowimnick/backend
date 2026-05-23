"""Business guest/student list filters contacts by business relationship."""
import pytest
from rest_framework import status

from quickstart.tests.factories import (
    BookingFactory,
    ContactFactory,
    ScheduleInstanceFactory,
    UserFactory,
)

API = "/api/business/students/"


def _emails(response):
    return {item.get("email") for item in response.json().get("results", [])}


@pytest.mark.django_db
class TestBusinessStudentListFilters:
    def test_excludes_class_page_contact_without_booking(
        self, business_owner_client, business
    ):
        ContactFactory(
            business=business,
            email="messenger@example.com",
            source="class_page_contact",
        )
        r = business_owner_client.get(API)
        assert r.status_code == status.HTTP_200_OK
        assert "messenger@example.com" not in _emails(r)

    def test_includes_contact_with_booking(
        self, business_owner_client, business, booking
    ):
        r = business_owner_client.get(API)
        assert r.status_code == status.HTTP_200_OK
        assert booking.contact.email in _emails(r)

    def test_includes_imported_contact_without_booking(
        self, business_owner_client, business
    ):
        ContactFactory(
            business=business,
            email="imported@example.com",
            source="import",
        )
        r = business_owner_client.get(API)
        assert r.status_code == status.HTTP_200_OK
        assert "imported@example.com" in _emails(r)

    def test_platform_guest_filter_requires_booking(
        self, business_owner_client, business
    ):
        platform_user = UserFactory(email="platform@example.com")
        ContactFactory(
            business=business,
            email=platform_user.email,
            user=platform_user,
            source="import",
        )
        r = business_owner_client.get(API, {"status_filter": "active"})
        assert r.status_code == status.HTTP_200_OK
        assert "platform@example.com" not in _emails(r)

    def test_platform_guest_filter_includes_booked_user(
        self, business_owner_client, business, schedule_instance
    ):
        platform_user = UserFactory(email="booked@example.com")
        contact = ContactFactory(
            business=business,
            email=platform_user.email,
            user=platform_user,
            source="platform_booking",
        )
        BookingFactory(
            schedule_instance=schedule_instance,
            user=platform_user,
            contact=contact,
        )
        r = business_owner_client.get(API, {"status_filter": "active"})
        assert r.status_code == status.HTTP_200_OK
        assert "booked@example.com" in _emails(r)

    def test_excludes_other_business_contacts(
        self, business_owner_client, business, other_business
    ):
        ContactFactory(business=other_business, email="other@example.com")
        r = business_owner_client.get(API)
        assert r.status_code == status.HTTP_200_OK
        assert "other@example.com" not in _emails(r)
