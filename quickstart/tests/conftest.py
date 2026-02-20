"""
Pytest configuration and shared fixtures for quickstart tests.
Uses pytest-django; each test runs in a transaction that is rolled back (default).
"""
import sys
import pytest


def pytest_collection_finish(session):
    """Print after collection so we know if hang is during collection vs run."""
    print("\n[pytest] Collection finished. Starting test run...", file=sys.stderr)
from django.test import RequestFactory
from rest_framework.test import APIClient

from quickstart.tests.factories import (
    UserFactory,
    BusinessFactory,
    ContactFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    BookingFactory,
    GiftCardFactory,
    BlogCategoryFactory,
    BlogPostFactory,
)


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
def request_factory():
    """Django RequestFactory for unit-testing views in isolation."""
    return RequestFactory()
