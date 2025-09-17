# quickstart/tests/test_views/test_business_class_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
import json
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from quickstart.models import (
    ClassesMain,
    ClassOption,
    BusinessInfo,
    Role,
    Booking,
    ScheduleInstance,
)
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    RoleFactory,
    ClassCategoryFactory,
    ClassSubcategoryFactory,
    ScheduleInstanceFactory,
    BookingFactory,
)


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class PublicCategoryViewsTest(APITestCase):
    """
    [NEW] Tests for the public category endpoints.
    """

    def setUp(self):
        self.cat1 = ClassCategoryFactory(name="Music")
        self.subcat1 = ClassSubcategoryFactory(category=self.cat1, name="Guitar")
        self.cat2 = ClassCategoryFactory(name="Art")
        self.subcat2 = ClassSubcategoryFactory(category=self.cat2, name="Painting")
        # This category has no classes and should not appear in the public list
        self.empty_cat = ClassCategoryFactory(name="Empty")

        # Assign a class to a subcategory to make it appear
        ClassesMainFactory(category=self.cat1, subcategory=self.subcat1)


class BusinessClassManagementTests(APITestCase):
    """
    Tests for a business owner managing their own classes, options, and schedules.
    """

    def setUp(self):
        self.owner = UserFactory()
        business_role = RoleFactory(name="Business Test Role")
        _get_and_assign_permission(business_role, "manage_own_classes", BusinessInfo)
        self.owner.role = business_role
        self.owner.save()
        self.owner.user_permissions.add(*business_role.permissions.all())
        self.business = BusinessInfoFactory(owner=self.owner)
        self.category = ClassCategoryFactory(name="Arts", key="arts")
        self.subcategory = ClassSubcategoryFactory(
            category=self.category, name="Pottery Making", key="pottery-making"
        )
        self.own_class = ClassesMainFactory(
            businessId=self.business,
            category=self.category,
            subcategory=self.subcategory,
        )
        self.own_option = ClassOptionFactory(classId=self.own_class)
        self.other_business = BusinessInfoFactory()
        self.other_class = ClassesMainFactory(businessId=self.other_business)
        self.client.force_authenticate(user=self.owner)

    def test_non_business_user_cannot_access(self):
        """
        Ensure a user without the 'manage_own_classes' permission gets a 403.
        """
        print("\n--- Running: test_non_business_user_cannot_access ---")
        non_business_user = UserFactory()
        self.client.force_authenticate(user=non_business_user)
        url = reverse("business-class-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: User without permission is correctly denied access.")

    def test_create_class_successfully(self):
        """
        POST /api/business/classes/ - A business owner can create a new class.
        """
        print("\n--- Running: test_create_class_successfully ---")
        url = reverse("business-class-list")

        option_data_string = json.dumps(
            [{"booking_type": "Single Session", "level": "beginner"}]
        )

        data = {
            "title": "New Pottery Class",
            "description": "Learn to make pottery. " * 15,
            "category_key": "arts",
            "subcategory_key": "pottery-making",
            "location": "Studio B",
            "coordinates": "40.7128,-74.0060",
            "image_s3_keys": [
                "originals/class_images/test1.jpg",
                "originals/class_images/test2.jpg",
            ],
            "cover_image_s3_key": "originals/class_images/test1.jpg",
            "options": option_data_string,
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(ClassesMain.objects.filter(title="New Pottery Class").exists())
        print("✅ PASSED: Business owner successfully created a new class.")

    def test_update_own_class(self):
        """
        PATCH /api/business/classes/{pk}/ - Owner can update their own class.
        """
        print("\n--- Running: test_update_own_class ---")
        url = reverse("business-class-detail", kwargs={"pk": self.own_class.pk})
        data = {"title": "Updated Class Title"}
        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.own_class.refresh_from_db()
        self.assertEqual(self.own_class.title, "Updated Class Title")
        print("✅ PASSED: Owner can update their own class.")

    def test_toggle_class_active_status(self):
        """
        [NEW TEST] PATCH /api/business/classes/{pk}/toggle-active/ - Owner can toggle class status.
        """
        print("\n--- Running: test_toggle_class_active_status ---")
        self.own_class.status = "active"
        self.own_class.save()
        url = reverse(
            "business-class-toggle-class-active", kwargs={"pk": self.own_class.pk}
        )

        # Deactivate
        response_off = self.client.patch(url, {}, format="json")
        self.assertEqual(response_off.status_code, status.HTTP_200_OK)
        self.assertEqual(response_off.data["status"], "inactive")

        # Reactivate
        response_on = self.client.patch(url, {}, format="json")
        self.assertEqual(response_on.status_code, status.HTTP_200_OK)
        self.assertEqual(response_on.data["status"], "active")
        print("✅ PASSED: Owner can successfully toggle a class's active status.")

    def test_list_only_own_classes(self):
        """
        GET /api/business/classes/ - A business owner should only see their own classes.
        """
        print("\n--- Running: test_list_only_own_classes ---")
        url = reverse("business-class-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        print("✅ PASSED: Business owner can list only their own classes.")

    def test_cannot_retrieve_other_business_class(self):
        """
        GET /api/business/classes/{pk}/ - Owner cannot retrieve another business's class.
        """
        print("\n--- Running: test_cannot_retrieve_other_business_class ---")
        url = reverse("business-class-detail", kwargs={"pk": self.other_class.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print("✅ PASSED: Owner correctly gets 404 for another business's class.")

    def test_delete_class_cancels_future_bookings_and_sets_for_refund(self):
        """
        DELETE /api/business/classes/{pk}/ - When a class is deleted (suspended),
        ensure future paid, confirmed bookings are cancelled and marked for refund.
        """
        print(
            "\n--- Running: test_delete_class_cancels_future_bookings_and_sets_for_refund ---"
        )
        # 1. Setup future and past bookings for the class
        future_instance = ScheduleInstanceFactory(
            schedule__option=self.own_option,
            date=timezone.now().date() + timedelta(days=10),
            price=Decimal("100.00"),
        )
        past_instance = ScheduleInstanceFactory(
            schedule__option=self.own_option,
            date=timezone.now().date() - timedelta(days=2),
        )

        future_booking = BookingFactory(
            schedule_instance=future_instance,
            status="confirmed",
            payment_status="paid",
            amount_paid=Decimal("100.00"),
        )
        past_booking = BookingFactory(
            schedule_instance=past_instance, status="completed"
        )

        # 2. Action: Delete the class
        url = reverse("business-class-detail", kwargs={"pk": self.own_class.pk})
        response = self.client.delete(url)

        # 3. Assertions
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Class is soft-deleted
        self.own_class.refresh_from_db()
        self.assertEqual(self.own_class.status, "suspended")

        # Future instance is hard-deleted
        self.assertFalse(
            ScheduleInstance.objects.filter(pk=future_instance.pk).exists()
        )

        # Future booking is cancelled and marked for refund
        future_booking.refresh_from_db()
        self.assertEqual(future_booking.status, "cancelled")
        self.assertEqual(future_booking.payment_status, "refund_pending")
        self.assertIn(
            "Session instance was removed", future_booking.cancellation_reason
        )

        # Past booking is unaffected
        past_booking.refresh_from_db()
        self.assertEqual(past_booking.status, "completed")

        print(
            "✅ PASSED: Deleting a class correctly handles future bookings and refunds."
        )


class BusinessScheduleManagementTests(APITestCase):
    def setUp(self):
        self.owner = UserFactory()
        business_role = RoleFactory(name="Business Test Role")
        _get_and_assign_permission(business_role, "manage_own_classes", BusinessInfo)
        self.owner.role = business_role
        self.owner.save()
        self.owner.user_permissions.add(*business_role.permissions.all())
        self.business = BusinessInfoFactory(owner=self.owner)
        self.klass = ClassesMainFactory(businessId=self.business)
        self.option = ClassOptionFactory(classId=self.klass)
        self.client.force_authenticate(user=self.owner)

    def test_create_schedule_for_own_class_option(self):
        """
        POST /api/business/schedules/ - Owner can create a schedule for their class option.
        """
        print("\n--- Running: test_create_schedule_for_own_class_option ---")
        url = reverse("business-schedule-list")
        future_date = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        data = {
            "option": self.option.optionId,
            "date": future_date,
            "time": "18:00",
            "duration": 90,
            "price": "35.00",
            "maxParticipants": 8,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.option.schedules.filter(date=future_date).exists())
        print("✅ PASSED: Owner can create a new schedule.")

    def test_bulk_create_schedules(self):
        """
        [NEW TEST] POST .../schedules/bulk-create/ - Can create multiple schedules at once.
        """
        print("\n--- Running: test_bulk_create_schedules ---")
        url = reverse("business-schedule-bulk-create")
        start_date = timezone.now().date() + timedelta(days=10)
        end_date = start_date + timedelta(days=7)  # Create for a week
        data = {
            "option": self.option.pk,
            "name": "Weekly Drop-in",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days_of_week": ["Mon", "Wed"],
            "times": ["10:00", "14:00"],
            "duration": 60,
            "price": "25.00",
            "maxParticipants": 15,
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["created_count"], 4)  # 2 days * 2 times
        self.assertEqual(self.option.schedules.count(), 4)
        print("✅ PASSED: Bulk schedule creation successful.")

    def test_group_delete_schedules(self):
        """
        [NEW TEST] POST .../schedules/group-delete/ - Can delete a named group of schedules.
        """
        print("\n--- Running: test_group_delete_schedules ---")
        ScheduleFactory.create_batch(5, option=self.option, name="Yoga Flow Tuesdays")
        self.assertEqual(self.option.schedules.count(), 5)

        url = reverse("business-schedule-group-delete")
        data = {"option_id": self.option.pk, "name": "Yoga Flow Tuesdays"}
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["message"],
            "Successfully deleted 5 schedules in group 'Yoga Flow Tuesdays'.",
        )
        self.assertEqual(self.option.schedules.count(), 0)
        print("✅ PASSED: Group delete for schedules successful.")


class BusinessScheduleInstanceManagementTests(APITestCase):
    def setUp(self):
        self.owner = UserFactory()
        business_role = RoleFactory(name="Business Test Role")
        _get_and_assign_permission(business_role, "manage_own_classes", BusinessInfo)
        self.owner.role = business_role
        self.owner.save()
        self.owner.user_permissions.add(*business_role.permissions.all())
        self.business = BusinessInfoFactory(owner=self.owner)
        self.klass = ClassesMainFactory(businessId=self.business)
        self.option = ClassOptionFactory(classId=self.klass)
        self.client.force_authenticate(user=self.owner)

    def test_business_can_cancel_future_instance(self):
        """
        [NEW TEST] POST .../schedule-instances/{pk}/cancel/ - Cancels instance and confirmed bookings.
        """
        print("\n--- Running: test_business_can_cancel_future_instance ---")
        instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() + timedelta(days=20),
        )
        booking = BookingFactory(schedule_instance=instance, status="confirmed")

        url = reverse("business-schedule-instance-cancel", kwargs={"pk": instance.pk})
        data = {"reason": "Instructor sick"}
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        instance.refresh_from_db()
        booking.refresh_from_db()

        self.assertEqual(instance.status, "cancelled")
        self.assertEqual(booking.status, "cancelled")
        self.assertIn("Session cancelled", booking.cancellation_reason)
        print(
            "✅ PASSED: Business successfully cancelled a future instance and its booking."
        )
