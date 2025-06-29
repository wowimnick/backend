# quickstart/tests/test_views/test_business_class_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
import json
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from datetime import timedelta

from quickstart.models import ClassesMain, ClassOption, BusinessInfo, Role
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassCategoryFactory,
    ClassesMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    RoleFactory,
    ClassSubcategoryFactory,
)


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class BusinessClassManagementTests(APITestCase):
    """
    Tests for a business owner managing their own classes, options, and schedules.
    """

    def setUp(self):
        self.owner = UserFactory()

        # Create a role and explicitly assign the necessary permissions.
        business_role = RoleFactory(name="Business Test Role")
        _get_and_assign_permission(business_role, "manage_own_classes", BusinessInfo)

        self.owner.role = business_role
        self.owner.save()

        self.category = ClassCategoryFactory(key="arts", name="Arts")
        # FIX: Create a subcategory to be used in tests.
        self.subcategory = ClassSubcategoryFactory(
            category=self.category, key="pottery-making", name="Pottery Making"
        )
        self.business = BusinessInfoFactory(
            owner=self.owner, classCategory=self.category
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

        # FIX: The test now correctly asserts that a 403 Forbidden is returned,
        # which is the proper behavior for a user lacking the required permission.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: User without permission is correctly denied access.")

    def test_create_class_successfully(self):
        """
        POST /api/business/classes/ - A business owner can create a new class.
        """
        print("\n--- Running: test_create_class_successfully ---")
        url = reverse("business-class-list")

        option_data = [{"booking_type": "Single Session", "level": "beginner"}]

        data = {
            "title": "New Pottery Class",
            "description": "Learn to make pottery. " * 15,
            "category_key": "arts",
            # FIX: The subcategory_key cannot be blank. Use the one created in setUp.
            "subcategory_key": "pottery-making",
            "location": "Studio B",
            "coordinates": "40.7128,-74.0060",
            # The _process_images_and_options method now expects 'images'
            "images": [
                SimpleUploadedFile(f"img{i}.jpg", b"content", "image/jpeg")
                for i in range(5)
            ],
            "options": json.dumps(option_data),
        }

        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(ClassesMain.objects.filter(title="New Pottery Class").exists())
        print("✅ PASSED: Business owner successfully created a new class.")

    def test_list_only_own_classes(self):
        """
        GET /api/business/classes/ - A business owner should only see their own classes.
        """
        print("\n--- Running: test_list_only_own_classes ---")
        url = reverse("business-class-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # FIX: The view is returning a raw list, not a paginated dictionary.
        # Adjust the assertion to check the length of the list directly.
        self.assertEqual(len(response.data), 1)
        print("✅ PASSED: Business owner can list only their own classes.")

    def test_cannot_retrieve_other_business_class(self):
        """
        GET /api/business/classes/{pk}/ - Owner cannot retrieve another business's class.
        """
        print("\n--- Running: test_cannot_retrieve_other_business_class ---")
        url = reverse("business-class-detail", kwargs={"pk": self.other_class.pk})
        response = self.client.get(url)
        # 404 is the correct response because the queryset for the view won't find it.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print("✅ PASSED: Owner correctly gets 404 for another business's class.")

    def test_update_own_class(self):
        """
        PATCH /api/business/classes/{pk}/ - Owner can update their own class.
        """
        print("\n--- Running: test_update_own_class ---")
        url = reverse("business-class-detail", kwargs={"pk": self.own_class.pk})
        data = {"title": "Updated Class Title"}
        response = self.client.patch(url, data)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.own_class.refresh_from_db()
        self.assertEqual(self.own_class.title, "Updated Class Title")
        print("✅ PASSED: Owner can update their own class.")

    def test_create_schedule_for_own_class_option(self):
        """
        POST /api/business/schedules/ - Owner can create a schedule for their class option.
        """
        print("\n--- Running: test_create_schedule_for_own_class_option ---")
        url = reverse("business-schedule-list")
        # FIX: Use a dynamic future date to avoid validation errors.
        future_date = (timezone.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        data = {
            "option": self.own_option.optionId,
            "date": future_date,
            "time": "18:00",
            "duration": 90,
            "price": "35.00",
            "maxParticipants": 8,
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.own_option.schedules.filter(date=future_date).exists())
        print("✅ PASSED: Owner can create a new schedule.")
