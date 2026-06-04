"""Admin API smoke tests."""
import csv
import io

import pytest
from rest_framework import status

from quickstart.tests.factories import BookingFactory, PaymentFactory, UserFactory

API = "/api"


@pytest.mark.django_db
class TestAdminUsersList:
    def test_superuser_can_list_users(self, admin_client):
        r = admin_client.get(f"{API}/admin/users/")
        assert r.status_code == status.HTTP_200_OK

    def test_regular_user_forbidden(self, api_client):
        api_client.force_authenticate(user=UserFactory(is_staff=False, is_superuser=False))
        r = api_client.get(f"{API}/admin/users/")
        assert r.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
class TestAdminBookingExport:
    """
    Regression tests for PYTHON-DJANGO-80:
    ValueError raised when calling queryset.iterator() without chunk_size on a
    queryset that has prefetch_related() applied.
    """

    def test_export_returns_csv(self, admin_client, booking):
        """Export endpoint returns a valid CSV with header and data rows."""
        r = admin_client.get(f"{API}/admin/bookings/export/")
        assert r.status_code == status.HTTP_200_OK
        assert "text/csv" in r["Content-Type"]

        content = b"".join(r.streaming_content) if hasattr(r, "streaming_content") else r.content
        reader = csv.reader(io.StringIO(content.decode("utf-8")))
        rows = list(reader)

        assert len(rows) >= 2, "Expected header row plus at least one data row"
        header = rows[0]
        assert header[0] == "Booking ID"
        assert "Status" in header
        assert "Payment Status" in header

        data_row = rows[1]
        assert str(booking.id) == data_row[0]

    def test_export_with_payment_attached(self, admin_client, booking):
        """Export correctly resolves payment intent ID from prefetched payments."""
        payment = PaymentFactory(booking=booking)

        r = admin_client.get(f"{API}/admin/bookings/export/")
        assert r.status_code == status.HTTP_200_OK

        content = b"".join(r.streaming_content) if hasattr(r, "streaming_content") else r.content
        reader = csv.reader(io.StringIO(content.decode("utf-8")))
        rows = list(reader)

        # Find the row for this booking and verify the payment intent ID is present.
        booking_rows = [row for row in rows[1:] if row and row[0] == str(booking.id)]
        assert len(booking_rows) == 1
        payment_intent_col = rows[0].index("Payment Intent ID")
        assert booking_rows[0][payment_intent_col] == payment.stripe_payment_intent_id

    def test_export_empty_queryset(self, admin_client):
        """Export returns only the header row when there are no bookings."""
        r = admin_client.get(f"{API}/admin/bookings/export/")
        assert r.status_code == status.HTTP_200_OK

        content = b"".join(r.streaming_content) if hasattr(r, "streaming_content") else r.content
        reader = csv.reader(io.StringIO(content.decode("utf-8")))
        rows = list(reader)
        assert len(rows) == 1, "Only the header row should be present with no bookings"

    def test_export_forbidden_without_permission(self, api_client):
        """Regular users without export permission receive 403."""
        regular_user = UserFactory(is_staff=False, is_superuser=False)
        api_client.force_authenticate(user=regular_user)
        r = api_client.get(f"{API}/admin/bookings/export/")
        assert r.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED)
