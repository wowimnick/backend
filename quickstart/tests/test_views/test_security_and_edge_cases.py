# quickstart/tests/test_views/test_security_and_edge_cases.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from quickstart.models import BusinessInfo
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    BookingFactory,
    RoleFactory,
    ClassCategoryFactory,
    ScheduleInstanceFactory,
)
from django.contrib.auth.models import Permission
from quickstart.utils.permissions import CanManageOwnClasses


class SecurityAndEdgeCaseTests(APITestCase):
    def setUp(self):
        # --- Setup for Cross-Tenant Access Test ---
        self.business_role = RoleFactory(name="Business Owner")
        permission = Permission.objects.get(codename="manage_own_classes")
        self.business_role.permissions.add(permission)

        # Owner A and their assets
        self.owner_A = UserFactory(role=self.business_role)
        self.owner_A.user_permissions.add(permission)
        self.business_A = BusinessInfoFactory(owner=self.owner_A)
        self.category_A = ClassCategoryFactory()
        self.class_A = ClassesMainFactory(
            businessId=self.business_A, category=self.category_A
        )

        # Owner B and their assets
        self.owner_B = UserFactory(role=self.business_role)
        self.owner_B.user_permissions.add(permission)
        self.business_B = BusinessInfoFactory(owner=self.owner_B)
        self.category_B = ClassCategoryFactory()
        self.class_B = ClassesMainFactory(
            businessId=self.business_B, category=self.category_B
        )

    def test_owner_A_cannot_edit_class_of_owner_B(self):
        """
        [SECURITY] An authenticated business owner cannot edit a class belonging to another owner.
        """
        # Authenticate as Owner A
        self.client.force_authenticate(user=self.owner_A)

        # Try to patch Owner B's class
        url = reverse("business-class-detail", kwargs={"pk": self.class_B.pk})
        data = {"title": "Hacked Title"}

        response = self.client.patch(url, data, format="json")

        # The queryset in `BusinessClassViewSet` should not find the class, resulting in a 404
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Verify the title of class_B was not changed
        self.class_B.refresh_from_db()
        self.assertNotEqual(self.class_B.title, "Hacked Title")

    def test_booking_last_spot_race_condition_simulation(self):
        """
        [EDGE CASE] Simulate two users trying to book the last spot.
        Note: This is a simplified simulation, not a true multi-threaded race condition test.
        """
        student1 = UserFactory()
        student2 = UserFactory()

        # Class with only one spot left
        instance = ScheduleInstanceFactory(max_participants=5)
        BookingFactory(schedule_instance=instance, status="confirmed", participants=4)
        self.assertEqual(instance.available_spots, 1)

        url = reverse("my-booking-list")
        data = {
            "selectedSlots": [{"id": instance.id}],
            "participants": 1,
            "participant_details": [{"name": "Final Attendee"}],
        }

        # Student 1 successfully books the last spot
        self.client.force_authenticate(user=student1)
        response1 = self.client.post(url, data, format="json")
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)

        # Student 2 immediately tries to book the same spot
        self.client.force_authenticate(user=student2)
        response2 = self.client.post(url, data, format="json")

        # Student 2 should be rejected because the class is now full
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Not enough spots", str(response2.data))

    def test_cannot_deactivate_business_with_future_bookings(self):
        """
        [EDGE CASE] A business owner cannot deactivate their profile if there are future confirmed bookings.
        """
        # Setup: Create a future, confirmed booking for the business
        future_date = timezone.now().date() + timedelta(days=10)
        schedule_instance = ScheduleInstanceFactory(
            schedule__option__classId=self.class_A, date=future_date
        )
        BookingFactory(schedule_instance=schedule_instance, status="confirmed")

        # Authenticate as the business owner
        self.client.force_authenticate(user=self.owner_A)
        url = reverse("my-business-profile")
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("future, confirmed bookings", response.data["detail"])

        # Verify the business is still active
        self.business_A.refresh_from_db()
        self.assertTrue(self.business_A.isActive)

    def test_can_deactivate_business_without_future_bookings(self):
        """
        [EDGE CASE] A business owner can deactivate their profile if there are no future confirmed bookings.
        """
        # Setup: Create a past booking and a future *cancelled* booking
        past_date = timezone.now().date() - timedelta(days=10)
        future_date = timezone.now().date() + timedelta(days=10)

        past_instance = ScheduleInstanceFactory(
            schedule__option__classId=self.class_A, date=past_date
        )
        future_instance = ScheduleInstanceFactory(
            schedule__option__classId=self.class_A, date=future_date
        )

        BookingFactory(schedule_instance=past_instance, status="completed")
        BookingFactory(schedule_instance=future_instance, status="cancelled")

        # Authenticate as the business owner
        self.client.force_authenticate(user=self.owner_A)
        url = reverse("my-business-profile")
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify the business has been soft-deleted (deactivated)
        self.business_A.refresh_from_db()
        self.assertFalse(self.business_A.isActive)
        self.assertEqual(self.business_A.verificationStatus, "closed")
