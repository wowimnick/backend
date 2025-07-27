import csv
from io import StringIO
from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import Payout, Role, Booking
from quickstart.tests.factories import (
    UserFactory,
    RoleFactory,
    PayoutFactory,
    BookingFactory,
    BusinessInfoFactory,
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


class AdminPayoutManagementTests(APITestCase):
    def setUp(self):
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        _get_and_assign_permissions(
            self.admin_role,
            {
                Payout: [
                    "access_payout_admin",
                    "view_payout_analytics",
                    "retry_failed_payout",
                    "trigger_manual_payout",
                    "export_payout_data",
                ],
            },
        )
        self.admin_user = UserFactory(role=self.admin_role)
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())
        self.non_admin_user = UserFactory()

        self.business1 = BusinessInfoFactory()
        self.booking1 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business1
        )
        self.booking2 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business1
        )

        self.paid_payout = PayoutFactory(
            business=self.business1, status="paid", amount=Decimal("120.50")
        )
        self.paid_payout.bookings.add(self.booking1)
        self.pending_payout = PayoutFactory(
            business=self.business1, status="pending", amount=Decimal("75.00")
        )
        self.pending_payout.bookings.add(self.booking2)
        self.failed_payout = PayoutFactory(
            business=self.business1, status="failed", amount=Decimal("99.99")
        )

        self.client.force_authenticate(user=self.admin_user)

    def test_admin_can_list_payouts(self):
        """
        GET /api/platform-admin/payouts/ - Admin can list all payouts.
        """
        url = reverse("admin-payouts-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 3)
        self.assertEqual(response.data["results"][2]["id"], str(self.paid_payout.id))

    def test_non_admin_cannot_access_payouts(self):
        """
        GET /api/platform-admin/payouts/ - Non-admin user is denied access.
        """
        self.client.force_authenticate(user=self.non_admin_user)
        url = reverse("admin-payouts-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_retrieve_payout_detail(self):
        """
        GET /api/platform-admin/payouts/{pk}/ - Admin can view payout details.
        """
        url = reverse("admin-payouts-detail", kwargs={"pk": self.paid_payout.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["amount"], "120.50")
        self.assertIn("bookings", response.data)
        self.assertEqual(len(response.data["bookings"]), 1)
        self.assertEqual(response.data["bookings"][0]["id"], self.booking1.id)

    def test_payout_analytics_endpoint(self):
        """
        GET /api/platform-admin/payouts/analytics/ - Admin can get payout analytics.
        """
        today = timezone.now().date()
        start_date = today - timedelta(days=5)

        # Create additional data within the test method's date range
        payouts_in_test = PayoutFactory.create_batch(
            2,
            status="paid",
            amount=Decimal("100.00"),
            created_at=timezone.now() - timedelta(days=2),
        )
        PayoutFactory(
            status="in_transit", created_at=timezone.now() - timedelta(days=3)
        )
        PayoutFactory(status="failed", created_at=timezone.now() - timedelta(days=1))

        url = reverse("admin-payouts-analytics")
        query_params = {
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": today.strftime("%Y-%m-%d"),
        }
        response = self.client.get(url, query_params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Corrected Assertions
        # Total Paid = 120.50 (from setUp) + 100.00 + 100.00 (from this test)
        self.assertEqual(response.data["total_paid_out"], Decimal("320.50"))

        # Pending = 1 (pending from setUp) + 1 (in_transit from this test)
        self.assertEqual(response.data["payouts_pending"], 2)

        # Failed = 1 (from setUp) + 1 (from this test)
        self.assertEqual(response.data["payouts_failed"], 2)

        # Average = (120.50 + 100 + 100) / 3
        expected_avg = (Decimal("120.50") + Decimal("100.00") * 2) / 3
        self.assertAlmostEqual(
            response.data["average_payout_amount"], expected_avg, places=2
        )

    def test_admin_can_retry_failed_payout(self):
        """
        POST /api/platform-admin/payouts/{pk}/retry/ - Admin can retry a failed payout.
        """
        url = reverse(
            "admin-payouts-retry-failed-payout", kwargs={"pk": self.failed_payout.pk}
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("Retry initiated", response.data["message"])

    def test_cannot_retry_non_failed_payout(self):
        """
        POST .../retry/ - Admin cannot retry a payout that hasn't failed.
        """
        url = reverse(
            "admin-payouts-retry-failed-payout", kwargs={"pk": self.paid_payout.pk}
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only failed payouts can be retried", response.data["error"])

    def test_admin_can_export_payouts_to_csv(self):
        """
        GET /api/platform-admin/payouts/export/ - Admin can export data as CSV.
        """
        url = reverse("admin-payouts-export")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertTrue(
            response["Content-Disposition"].startswith(
                'attachment; filename="payouts_export_'
            )
        )
        content = response.content.decode("utf-8")
        csv_reader = csv.reader(StringIO(content))
        rows = list(csv_reader)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][0], "Payout ID")
        self.assertEqual(rows[0][2], "Business Name")
        self.assertEqual(rows[3][5], self.paid_payout.status)
