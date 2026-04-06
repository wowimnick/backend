"""
Pytest configuration and shared fixtures for quickstart tests.
Uses pytest-django; each test runs in a transaction that is rolled back (default).
"""
import sys

import pytest
from django.test import RequestFactory
from rest_framework.test import APIClient

from quickstart.models import BusinessStaff
from quickstart.tests.factories import (
    BlogCategoryFactory,
    BlogPostFactory,
    BookingFactory,
    BusinessFactory,
    BusinessRoleFactory,
    BusinessStaffFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ContactFactory,
    GiftCardFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    UserFactory,
)


def pytest_collection_finish(session):
    """Print after collection so we know if hang is during collection vs run."""
    print("\n[pytest] Collection finished. Starting test run...", file=sys.stderr)


@pytest.fixture
def api_client():
    """DRF API client for unauthenticated requests (e.g. public endpoints)."""
    return APIClient()


@pytest.fixture
def authenticated_client(api_client, user):
    """API client authenticated as the given user."""
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def user():
    """A regular user (for auth tests)."""
    return UserFactory()


@pytest.fixture
def business():
    """An active, verified business (for public list/retrieve and widget)."""
    return BusinessFactory(
        isActive=True,
        verificationStatus="verified",
        slug="test-business-slug",
    )


@pytest.fixture
def public_class(business):
    """A class belonging to the given business, for public class endpoints."""
    return ClassMainFactory(
        businessId=business,
        status="active",
        slug="test-class-slug",
    )


@pytest.fixture
def widget_business(business):
    """Business with widget_api_key for widget endpoint tests."""
    return business  # Factory already sets widget_api_key via default uuid


@pytest.fixture
def business_owner_client(api_client, business):
    """API client authenticated as the business owner (my-business/*, business router)."""
    api_client.force_authenticate(user=business.owner)
    return api_client


@pytest.fixture
def other_business():
    """Second business + owner for cross-tenant isolation tests."""
    owner = UserFactory()
    return BusinessFactory(owner=owner)


@pytest.fixture
def admin_client(api_client):
    """API client authenticated as Django superuser (admin API)."""
    admin = UserFactory(is_staff=True, is_superuser=True)
    api_client.force_authenticate(user=admin)
    return api_client


@pytest.fixture
def staff_member(business):
    """Accepted BusinessStaff row for `business` (separate user from owner)."""
    role = BusinessRoleFactory(business=business, name="Test Staff Role")
    staff_user = UserFactory()
    return BusinessStaffFactory(
        business=business,
        role=role,
        user=staff_user,
        invited_email=staff_user.email,
        status=BusinessStaff.StaffStatus.ACCEPTED,
        invited_by=business.owner,
    )


@pytest.fixture
def staff_client(api_client, staff_member):
    """API client authenticated as a staff user of the business."""
    api_client.force_authenticate(user=staff_member.user)
    return api_client


@pytest.fixture
def schedule_instance(business):
    """A future schedule instance under a class owned by `business`."""
    cls = ClassMainFactory(businessId=business, status="active")
    option = ClassOptionFactory(classId=cls)
    schedule = ScheduleFactory(option=option)
    return ScheduleInstanceFactory(schedule=schedule)


@pytest.fixture
def booking(business, schedule_instance):
    """A confirmed booking; contact is linked to `business`."""
    contact = ContactFactory(business=business)
    return BookingFactory(
        schedule_instance=schedule_instance,
        contact=contact,
    )


@pytest.fixture
def gift_card():
    """Standalone gift card row."""
    return GiftCardFactory()


@pytest.fixture
def contact(business):
    """CRM contact for `business`."""
    return ContactFactory(business=business)


@pytest.fixture
def request_factory():
    """Django RequestFactory for unit-testing views in isolation."""
    return RequestFactory()
