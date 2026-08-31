"""Business CRM contacts API."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from rest_framework import status

from quickstart.services.crm_stats import refresh_contact_stats
from quickstart.tests.factories import (
    BookingFactory,
    ContactFactory,
    ScheduleInstanceFactory,
    UserFactory,
)

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

    def test_list_includes_crm_stats_fields(self, business_owner_client, business):
        _grant_dashboard_access(business.owner)
        ContactFactory(
            business=business,
            first_name="Maya",
            lifetime_value=Decimal("80.00"),
            booking_count=2,
            tags=["vip"],
            status="active",
        )
        r = business_owner_client.get(f"{API}/my-business/contacts/")
        assert r.status_code == status.HTTP_200_OK
        row = next(c for c in r.json()["results"] if c.get("first_name") == "Maya")
        assert row["lifetime_value"] == "80.00"
        assert row["booking_count"] == 2
        assert row["tags"] == ["vip"]
        assert row["status"] == "active"
        assert "last_booking_at" in row
        assert "last_activity_at" in row


@pytest.mark.django_db
class TestRefreshContactStats:
    def test_recomputes_from_confirmed_paid_bookings(self, business):
        contact = ContactFactory(business=business)
        instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=business
        )
        BookingFactory(
            contact=contact,
            schedule_instance=instance,
            amount_paid=Decimal("40.00"),
            status="confirmed",
            payment_status="paid",
        )
        BookingFactory(
            contact=contact,
            schedule_instance=instance,
            amount_paid=Decimal("10.00"),
            status="cancelled",
            payment_status="paid",
        )
        refresh_contact_stats(contact)
        contact.refresh_from_db()
        assert contact.lifetime_value == Decimal("40.00")
        assert contact.booking_count == 1
        assert contact.first_booking_at is not None
        assert contact.last_booking_at is not None
        assert contact.last_activity_at == contact.last_booking_at


@pytest.mark.django_db
class TestBackfillContactStats:
    def test_links_orphan_booking_and_refreshes_stats(self, business):
        user = UserFactory(email="orphan.booker@example.com")
        instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=business
        )
        booking = BookingFactory(
            user=user,
            contact=None,
            schedule_instance=instance,
            amount_paid=Decimal("25.00"),
            status="confirmed",
            payment_status="paid",
        )
        call_command("backfill_contact_stats")
        booking.refresh_from_db()
        assert booking.contact is not None
        assert booking.contact.email.lower() == user.email.lower()
        assert booking.contact.business_id == business.pk
        assert booking.contact.lifetime_value == Decimal("25.00")
        assert booking.contact.booking_count == 1

