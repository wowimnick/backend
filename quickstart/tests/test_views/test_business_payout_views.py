# quickstart/tests/test_views/test_business_payout_views.py

import csv
from io import StringIO
from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from decimal import Decimal

from quickstart.models import BusinessInfo, Role, Payment
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
    BookingFactory,
    PaymentFactory,
    PayoutFactory,
)


class BusinessPayoutViewSetTests(APITestCase):
    """
    [NEW] Tests for the business-facing payout views.
    """

    def setUp(self):
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        self.user.role = business_role
        self.user.save()
        # Note: IsBusinessOwnerOrManager permission is used, which doesn't require a codename
        self.business = BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)

        # Create data
        self.booking1 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            user_facing_reference="BKG-PAYOUT1",
        )
        self.booking2 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            status="completed",
            payment_status="paid",
            payout_status="pending",
        )
        self.payment2 = PaymentFactory(
            booking=self.booking2, net_payout_amount=Decimal("85.00")
        )

        self.payout = PayoutFactory(business=self.business, amount=Decimal("150.00"))
        self.payout.bookings.add(self.booking1)

    def test_business_owner_can_list_payouts(self):
        """
        GET /api/business/payouts/ - An owner can list their business payouts.
        """
        print("\n--- Running: test_business_owner_can_list_payouts ---")
        url = reverse("business-payout-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["amount"], "150.00")
        print("✅ PASSED: Business owner can list payouts.")

    def test_business_owner_can_view_bookings_for_payout(self):
        """
        GET /api/business/payouts/{pk}/bookings/ - An owner can view bookings in a payout.
        """
        print("\n--- Running: test_business_owner_can_view_bookings_for_payout ---")
        url = reverse("business-payout-bookings", kwargs={"pk": self.payout.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            response.data["results"][0]["user_facing_reference"],
            self.booking1.user_facing_reference,
        )
        print("✅ PASSED: Business owner can view bookings for a specific payout.")

    def test_business_owner_can_get_payout_summary(self):
        """
        GET /api/business/payouts/summary/ - An owner can get their financial summary.
        """
        print("\n--- Running: test_business_owner_can_get_payout_summary ---")
        url = reverse("business-payout-summary")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["pending_payout_amount"], "85.00"
        )  # From payment2
        self.assertEqual(response.data["stripe_account_status"], "active")
        print("✅ PASSED: Business owner can get payout summary.")

    def test_business_owner_can_export_payout_details_csv(self):
        """
        GET /api/business/payouts/{pk}/export/ - An owner can export a payout report.
        """
        print("\n--- Running: test_business_owner_can_export_payout_details_csv ---")
        # Add a payment record for the booking in the payout for a more complete export
        PaymentFactory(booking=self.booking1)
        url = reverse(
            "business-payout-export-payout-details", kwargs={"pk": self.payout.pk}
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv")

        # Basic check of CSV content
        content = response.content.decode("utf-8")
        self.assertIn("Payout Reconciliation Report", content)
        self.assertIn(self.booking1.user_facing_reference, content)
        print("✅ PASSED: Business owner can export payout details as CSV.")
