# quickstart/tests/test_views/test_admin_booking_payment_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission
from unittest.mock import patch, MagicMock
from decimal import Decimal
from django.test import override_settings
from django.conf import settings

from quickstart.models import Booking, Payment, Role
from quickstart.tests.factories import (
    UserFactory,
    BookingFactory,
    PaymentFactory,
    RoleFactory,
)


def _get_and_assign_permissions(role, permissions_map):
    for model_class, codenames in permissions_map.items():
        content_type = ContentType.objects.get_for_model(model_class)
        for codename in codenames:
            permission, _ = Permission.objects.get_or_create(
                codename=codename,
                content_type=content_type,
            )
            role.permissions.add(permission)


@override_settings(
    MIDDLEWARE=[mw for mw in settings.MIDDLEWARE if "silk.middleware" not in mw]
)
class AdminBookingPaymentTests(APITestCase):
    def setUp(self):
        # FIX: The patch was targeting a non-existent signal and was not used
        # by the tests in this class. It has been removed to resolve the AttributeError.
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        _get_and_assign_permissions(
            self.admin_role,
            {
                Booking: ["access_booking_admin", "view_booking", "cancel_any_booking"],
                Payment: ["access_payment_admin", "view_payment", "process_refund"],
            },
        )
        self.admin_user = UserFactory(role=self.admin_role)
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())

        # Create some data
        self.booking1 = BookingFactory(status="confirmed", payment_status="paid")
        self.payment1 = PaymentFactory(
            booking=self.booking1,
            status="succeeded",
            amount=Decimal("100.00"),
            refunded_amount=Decimal("0.00"),
        )
        self.booking2 = BookingFactory(status="pending", payment_status="pending")

        self.client.force_authenticate(user=self.admin_user)

    def test_admin_can_list_all_bookings(self):
        """
        GET /api/platform-admin/bookings/ - Admin can list all bookings.
        """
        print("\n--- Running: test_admin_can_list_all_bookings ---")
        url = reverse("admin-bookings-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        print("✅ PASSED: Admin can list all bookings.")

    @patch(
        "quickstart.views.admin.booking_management.booking_views.send_booking_cancelled_by_other_email"
    )
    def test_admin_can_cancel_booking(self, mock_send_email):
        """
        POST .../cancel/ - Admin can cancel a confirmed booking.
        """
        print("\n--- Running: test_admin_can_cancel_booking ---")
        url = reverse("admin-bookings-cancel", kwargs={"pk": self.booking1.pk})
        data = {"reason": "Cancelled by admin due to system maintenance."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.booking1.refresh_from_db()
        self.assertEqual(self.booking1.status, "cancelled")
        # Since it was paid, it should be marked for refund
        self.assertEqual(self.booking1.payment_status, "refund_pending")
        mock_send_email.assert_called_once()
        print("✅ PASSED: Admin can cancel a booking.")

    def test_admin_cannot_cancel_completed_booking(self):
        """
        [EDGE CASE] POST .../cancel/ - Admin cannot cancel a booking that is already completed.
        """
        print("\n--- Running: test_admin_cannot_cancel_completed_booking ---")
        completed_booking = BookingFactory(status="completed")
        url = reverse("admin-bookings-cancel", kwargs={"pk": completed_booking.pk})
        data = {"reason": "This should fail."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot be cancelled", response.data["error"])
        completed_booking.refresh_from_db()
        self.assertEqual(completed_booking.status, "completed")
        print("✅ PASSED: Admin correctly blocked from cancelling a completed booking.")

    def test_admin_can_list_all_payments(self):
        """
        GET /api/platform-admin/payments/ - Admin can list all payments.
        """
        print("\n--- Running: test_admin_can_list_all_payments ---")
        url = reverse("admin-payments-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        print("✅ PASSED: Admin can list all payments.")

    @patch("stripe.Refund.create")
    def test_admin_can_process_refund(self, mock_stripe_refund):
        """
        POST .../refund/ - Admin can process a full refund for a payment.
        """
        print("\n--- Running: test_admin_can_process_refund ---")
        # Mock the Stripe API response
        mock_stripe_refund.return_value = MagicMock(id="re_12345")

        url = reverse("admin-payments-refund", kwargs={"pk": self.payment1.pk})
        data = {"reason": "requested_by_customer"}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.payment1.refresh_from_db()
        self.assertEqual(self.payment1.status, "refunded")
        self.assertEqual(self.payment1.refunded_amount, self.payment1.amount)
        # Check that the related booking's payment status was also updated
        self.booking1.refresh_from_db()
        self.assertEqual(self.booking1.payment_status, "refunded")
        mock_stripe_refund.assert_called_once()
        print("✅ PASSED: Admin can process a refund.")

    @patch("stripe.Refund.create")
    def test_admin_cannot_refund_more_than_available(self, mock_stripe_refund):
        """
        [EDGE CASE] POST .../refund/ - Admin cannot refund more than the available amount.
        """
        print("\n--- Running: test_admin_cannot_refund_more_than_available ---")
        # Make a partial refund first
        self.payment1.refunded_amount = Decimal("20.00")
        self.payment1.status = "partially_refunded"
        self.payment1.save()

        url = reverse("admin-payments-refund", kwargs={"pk": self.payment1.pk})
        # Try to refund 90.00, but only 80.00 is available
        data = {"amount": "90.00", "reason": "requested_by_customer"}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeds available amount", response.data["error"])
        mock_stripe_refund.assert_not_called()
        print("✅ PASSED: Admin correctly blocked from over-refunding.")

    def test_booking_list_is_performant_and_avoids_n1_queries(self):
        """
        [PERFORMANCE] GET .../bookings/ - Ensures the admin booking list is efficient.
        """
        print(
            "\n--- Running: test_booking_list_is_performant_and_avoids_n1_queries ---"
        )
        BookingFactory.create_batch(18)
        self.assertEqual(Booking.objects.count(), 20)
        url = reverse("admin-bookings-list")

        with self.settings(SILKY_META=False):
            with self.assertNumQueries(5):
                response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 10)
        print("✅ PASSED: Admin booking list endpoint is performant.")
