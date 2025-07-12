# quickstart/tests/test_views/test_business_management_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
import json

from quickstart.models import BusinessInfo
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
)


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class BusinessManagementTests(APITestCase):
    """
    Tests for business registration, profile management, and dashboard access.
    """

    def setUp(self):
        self.user = UserFactory()
        self.other_user = UserFactory()

        # Create a role and explicitly assign the necessary permissions for these tests.
        self.business_owner_role = RoleFactory(name="Business Owner")
        _get_and_assign_permission(
            self.business_owner_role, "access_business_dashboard", BusinessInfo
        )
        _get_and_assign_permission(
            self.business_owner_role, "manage_own_business_profile", BusinessInfo
        )

        self.user.role = self.business_owner_role
        self.user.save()
        self.user.user_permissions.add(*self.business_owner_role.permissions.all())

        # Base data for a valid registration
        self.valid_data = {
            "businessName": "Test Fitness Studio",
            "businessType": "studio",
            "businessDescription": "A great place to work out and get fit. " * 10,
            "openingTime": "09:00",
            "closingTime": "21:00",
            "liabilityWaiver": True,
            "business_timezone": "America/New_York",
            "studentContactPhone": "+15551234567",
            "studentContactEmail": "contact@testfitness.com",
            "preferredContact": "email",
            "contact_privacy": "public",
            "businessAddress": "123 Fitness Lane",
            "businessCity": "Fitville",
            "businessState": "CA",
            "businessZipCode": "90210",
            "termsAccepted": True,
            "privacyAccepted": True,
        }

    def test_successful_business_registration(self):
        """
        POST /api/business/register/ - A logged-in user can register a new business.
        """
        print("\n--- Running: test_successful_business_registration ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("business-register")

        response = self.client.post(url, self.valid_data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(BusinessInfo.objects.filter(owner=self.user).exists())
        business = BusinessInfo.objects.get(owner=self.user)
        self.assertEqual(business.businessName, "Test Fitness Studio")
        print("✅ PASSED: Successful business registration.")

    def test_registration_fails_with_invalid_timezone(self):
        """
        [EDGE CASE] POST /api/business/register/ - Registration fails with an invalid timezone.
        """
        print("\n--- Running: test_registration_fails_with_invalid_timezone ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("business-register")
        data = self.valid_data.copy()
        data["business_timezone"] = "Mars/Olympus_Mons"

        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("business_timezone", response.data["error"])
        print("✅ PASSED: Registration correctly failed for invalid timezone.")

    def test_cannot_register_second_business(self):
        """
        POST /api/business/register/ - A user who already owns a business cannot register another.
        """
        print("\n--- Running: test_cannot_register_second_business ---")
        # Create a first business for the user
        BusinessInfoFactory(owner=self.user)

        self.client.force_authenticate(user=self.user)
        url = reverse("business-register")
        data = {
            "businessName": "Second Business"
        }  # Incomplete data is fine, it should fail before validation

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        print("✅ PASSED: User prevented from registering a second business.")

    def test_owner_can_view_own_business_profile(self):
        """
        GET /api/my-business/profile/ - An owner can view their business profile.
        """
        print("\n--- Running: test_owner_can_view_own_business_profile ---")
        business = BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        url = reverse("my-business-profile")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["businessId"], business.businessId)
        print("✅ PASSED: Owner can view their own business profile.")

    def test_random_user_cannot_view_business_profile(self):
        """
        GET /api/my-business/profile/ - A non-owner/manager cannot access the profile view.
        """
        print("\n--- Running: test_random_user_cannot_view_business_profile ---")
        BusinessInfoFactory(owner=self.user)  # A business exists
        self.client.force_authenticate(
            user=self.other_user
        )  # Authenticate as someone else
        url = reverse("my-business-profile")

        response = self.client.get(url)
        # It will raise a 404 because the get_object query finds nothing for this user.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print("✅ PASSED: Random user cannot view another's business profile.")

    def test_owner_can_update_own_business_profile(self):
        """
        PATCH /api/my-business/profile/ - An owner can update their business profile.
        """
        print("\n--- Running: test_owner_can_update_own_business_profile ---")
        business = BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        url = reverse("my-business-profile")

        update_data = {
            "businessName": "Updated Name Fitness",
            "tags_keywords": json.dumps(["cardio", "weights"]),
        }
        response = self.client.patch(url, update_data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        business.refresh_from_db()
        self.assertEqual(business.businessName, "Updated Name Fitness")
        self.assertEqual(business.tags_keywords, ["cardio", "weights"])
        print("✅ PASSED: Owner can update their business profile.")

    def test_owner_can_view_dashboard(self):
        """
        GET /api/my-business/overview/ - An owner can view their business dashboard.
        """
        print("\n--- Running: test_owner_can_view_dashboard ---")
        BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        url = reverse("my-business-overview")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("metrics", response.data)
        self.assertIn("revenue_trend", response.data)
        self.assertIn("setup_progress", response.data)
        print("✅ PASSED: Owner can view their business dashboard.")

    def test_regular_user_cannot_view_dashboard(self):
        """
        GET /api/my-business/overview/ - A regular user cannot access the dashboard.
        """
        print("\n--- Running: test_regular_user_cannot_view_dashboard ---")
        self.client.force_authenticate(user=self.other_user)
        url = reverse("my-business-overview")

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: Regular user is denied access to the business dashboard.")

    def test_removed_manager_cannot_access_dashboard(self):
        """
        [EDGE CASE] GET .../overview/ - A user who was a manager but was removed can no longer access the dashboard.
        """
        print("\n--- Running: test_removed_manager_cannot_access_dashboard ---")
        business = BusinessInfoFactory(owner=self.user)
        manager_user = UserFactory()
        business.managers.add(manager_user)
        manager_user.user_permissions.add(
            Permission.objects.get(codename="access_business_dashboard")
        )

        # 1. Verify manager CAN access
        self.client.force_authenticate(user=manager_user)
        url = reverse("my-business-overview")
        response_allowed = self.client.get(url)
        self.assertEqual(response_allowed.status_code, status.HTTP_200_OK)

        # 2. Remove manager from the business
        business.managers.remove(manager_user)

        # 3. Verify manager CANNOT access anymore
        response_denied = self.client.get(url)
        self.assertEqual(response_denied.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: Removed manager correctly denied access to dashboard.")
