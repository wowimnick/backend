# quickstart/tests/test_views/test_business_booking_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth.models import Permission

from quickstart.models import BusinessInfo, Role
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
    BookingFactory,
    ScheduleInstanceFactory,
)


class BusinessBookingViewSetTests(APITestCase):
    """
    [NEW] Tests for the business-facing booking views, including analytics and rescheduling.
    """

    def setUp(self):
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        # Add a placeholder permission
        permission = Permission.objects.get(codename="access_business_dashboard")
        business_role.permissions.add(permission)
        self.user.role = business_role
        self.user.save()
        self.user.user_permissions.add(permission)

        self.business = BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)

        # Create data
        self.booking = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            schedule_instance__price=Decimal("100.00"),
            schedule_instance__date=timezone.now().date()
            - timedelta(days=1),  # Make it past
            status="confirmed",
            payment_status="paid",
            amount_paid=Decimal("100.00"),
        )
        self.other_booking = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            status="completed",
            participants=2,
            payment_status="paid",
            amount_paid=Decimal("50.00"),
        )

    def test_get_available_slots_for_reschedule(self):
        """
        GET /api/business/bookings/{pk}/available-slots/ - Test retrieval of valid reschedule slots.
        """
        print("\n--- Running: test_get_available_slots_for_reschedule ---")
        # A valid slot with enough capacity
        valid_slot = ScheduleInstanceFactory(
            schedule=self.booking.schedule_instance.schedule,
            date=timezone.now().date() + timedelta(days=5),
            max_participants=10,
        )
        # A slot that is already full
        full_slot = ScheduleInstanceFactory(
            schedule=self.booking.schedule_instance.schedule,
            date=timezone.now().date() + timedelta(days=6),
            max_participants=2,
        )
        BookingFactory(schedule_instance=full_slot, participants=2)

        url = reverse(
            "business-booking-available-slots", kwargs={"pk": self.booking.pk}
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], valid_slot.id)
        self.assertTrue(response.data[0]["is_valid"])
        print("✅ PASSED: Correctly identified available slots for rescheduling.")

    def test_reschedule_dry_run(self):
        """
        POST /api/business/bookings/{pk}/reschedule/ - Test the dry_run functionality.
        """
        print("\n--- Running: test_reschedule_dry_run ---")
        new_instance = ScheduleInstanceFactory(
            schedule=self.booking.schedule_instance.schedule,
            date=timezone.now().date() + timedelta(days=8),
            price=Decimal("120.00"),  # Different price
        )
        url = reverse("business-booking-reschedule", kwargs={"pk": self.booking.pk})
        data = {
            "new_schedule_instance_id": new_instance.id,
            "dry_run": True,
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "check_success")
        self.assertEqual(response.data["price_difference"], Decimal("20.00"))
        self.assertEqual(response.data["new_price"], new_instance.price)

        # Ensure the booking was NOT actually changed
        self.booking.refresh_from_db()
        self.assertNotEqual(self.booking.schedule_instance, new_instance)
        print(
            "✅ PASSED: Reschedule dry run correctly calculated differences without saving."
        )

    def test_get_booking_analytics(self):
        """
        GET /api/business/bookings/analytics/ - Test the booking analytics endpoint.
        """
        print("\n--- Running: test_get_booking_analytics ---")
        permission = Permission.objects.get(codename="view_own_booking_analytics")
        self.user.user_permissions.add(permission)
        url = reverse("business-booking-analytics")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("summary", response.data)
        self.assertIn("trends", response.data)
        self.assertIn("class_insights", response.data)

        summary = response.data["summary"]
        self.assertEqual(summary["total_booking_transactions"], 2)
        self.assertEqual(summary["total_participant_spots"], 3)  # 1 + 2
        self.assertEqual(summary["active_booking_transactions"], 1)
        self.assertEqual(summary["completed_booking_transactions"], 1)
        # Revenue is from both confirmed and completed bookings with payment_status='paid'
        self.assertEqual(summary["total_revenue"], 150.00)
        print("✅ PASSED: Booking analytics endpoint returned correct summary data.")
