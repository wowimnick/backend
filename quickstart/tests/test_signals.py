"""Signal handlers (smoke: model lifecycle)."""
import pytest

from quickstart.tests.factories import BookingFactory


@pytest.mark.django_db
def test_booking_factory_creates_successfully():
    BookingFactory()
