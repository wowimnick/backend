# quickstart/tests/test_views/test_business_management_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
import json
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from quickstart.models import BusinessInfo, BusinessRole, BusinessStaff, Discount
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
    ClassesMainFactory,
    ClassOptionFactory,
    ClassCategoryFactory,
)


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class PresignedURLTests(APITestCase):
    def setUp(self):
        self.user = UserFactory()
        self.client.force_authenticate(user=self.user)
        self.url = reverse("generate-upload-url")

    def test_generate_presigned_url_for_valid_type(self):
        """
        [NEW TEST] POST /api/business/generate-upload-url/ - Should succeed for a valid uploadType.
        """
        print("\n--- Running: test_generate_presigned_url_for_valid_type ---")
        data = {
            "fileName": "avatar.jpg",
            "contentType": "image/jpeg",
            "uploadType": "class_image",
        }
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("url", response.data)
        self.assertIn("fields", response.data)
        self.assertTrue(response.data["s3_key"].startswith("originals/class_images/"))
        print("✅ PASSED: Successfully generated presigned URL for a valid type.")

    def test_generate_presigned_url_for_invalid_type_fails(self):
        """
        [NEW TEST] POST .../generate-upload-url/ - Should fail for an invalid uploadType.
        """
        print("\n--- Running: test_generate_presigned_url_for_invalid_type_fails ---")
        data = {
            "fileName": "document.pdf",
            "contentType": "application/pdf",
            "uploadType": "tax_document",  # Not in the allowed list
        }
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid uploadType", response.data["error"])
        print(
            "✅ PASSED: Correctly rejected presigned URL request for an invalid type."
        )


class BusinessManagementTests(APITestCase):
    """
    Tests for business registration, profile management, and dashboard access.
    """

    def setUp(self):
        self.user = UserFactory()
        self.other_user = UserFactory()
        self.business_owner_role = RoleFactory(name="Business Owner")
        _get_and_assign_permission(
            self.business_owner_role, "access_business_dashboard", BusinessInfo
        )
        _get_and_assign_permission(
            self.business_owner_role, "manage_own_business_profile", BusinessInfo
        )
        _get_and_assign_permission(
            self.business_owner_role, "manage_own_business_discounts", BusinessInfo
        )
        self.user.role = self.business_owner_role
        self.user.save()
        self.user.user_permissions.add(*self.business_owner_role.permissions.all())
        self.valid_data = {
            "businessName": "Test Fitness Studio",
            "businessType": "studio",
            "businessDescription": "A great place to work out and get fit. " * 10,
            "businessHours": json.dumps(
                [
                    {
                        "day": "Monday",
                        "isOpen": True,
                        "open": "09:00",
                        "close": "21:00",
                    },
                    {
                        "day": "Tuesday",
                        "isOpen": True,
                        "open": "09:00",
                        "close": "21:00",
                    },
                    {
                        "day": "Wednesday",
                        "isOpen": True,
                        "open": "09:00",
                        "close": "21:00",
                    },
                    {
                        "day": "Thursday",
                        "isOpen": True,
                        "open": "09:00",
                        "close": "21:00",
                    },
                    {
                        "day": "Friday",
                        "isOpen": True,
                        "open": "09:00",
                        "close": "21:00",
                    },
                    {
                        "day": "Saturday",
                        "isOpen": True,
                        "open": "10:00",
                        "close": "18:00",
                    },
                    {
                        "day": "Sunday",
                        "isOpen": False,
                        "open": "10:00",
                        "close": "18:00",
                    },
                ]
            ),
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
            # MODIFIED: Add the S3 key to the payload
            "businessImage_s3_key": "originals/business_images/test-biz-img.jpg",
        }

    def test_successful_business_registration(self):
        """
        POST /api/business/register/ - A logged-in user can register a new business.
        """
        print("\n--- Running: test_successful_business_registration ---")
        self.client.force_authenticate(user=self.user)
        url = reverse("business-register")

        response = self.client.post(url, self.valid_data, format="json")

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

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("business_timezone", response.data["error"])
        print("✅ PASSED: Registration correctly failed for invalid timezone.")

    def test_cannot_register_second_business(self):
        """
        POST /api/business/register/ - A user who already owns a business cannot register another.
        """
        print("\n--- Running: test_cannot_register_second_business ---")
        BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.user)
        url = reverse("business-register")
        data = {"businessName": "Second Business"}
        response = self.client.post(url, data, format="json")
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
        BusinessInfoFactory(owner=self.user)
        self.client.force_authenticate(user=self.other_user)
        url = reverse("my-business-profile")
        response = self.client.get(url)
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
            "tags_keywords": ["cardio", "weights"],
            "businessImage": "originals/business_images/new-image.png",
        }

        response = self.client.patch(url, update_data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        business.refresh_from_db()
        self.assertEqual(business.businessName, "Updated Name Fitness")
        self.assertEqual(business.tags_keywords, ["cardio", "weights"])
        self.assertEqual(
            business.businessImage.name, "originals/business_images/new-image.png"
        )
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

    def test_dashboard_stats_actions(self):
        """
        [NEW TEST] GET /api/business-stats/{pk}/... - Test the dashboard stats actions.
        """
        print("\n--- Running: test_dashboard_stats_actions ---")
        business = BusinessInfoFactory(owner=self.user)
        # Add the required permission for these specific endpoints
        permission = Permission.objects.get(codename="access_business_dashboard")
        self.user.user_permissions.add(permission)
        self.client.force_authenticate(user=self.user)

        # Test dashboard_stats
        url_stats = reverse("business-stats-dashboard", kwargs={"pk": business.pk})
        res_stats = self.client.get(url_stats)
        self.assertEqual(res_stats.status_code, status.HTTP_200_OK)
        self.assertIn("total_students", res_stats.data)

        # Test revenue_over_time
        url_revenue = reverse("business-stats-revenue", kwargs={"pk": business.pk})
        res_revenue = self.client.get(url_revenue)
        self.assertEqual(res_revenue.status_code, status.HTTP_200_OK)
        self.assertIsInstance(res_revenue.data, list)

        # Test class_performance
        url_perf = reverse("business-stats-class-perf", kwargs={"pk": business.pk})
        res_perf = self.client.get(url_perf)
        self.assertEqual(res_perf.status_code, status.HTTP_200_OK)
        self.assertIsInstance(res_perf.data, list)
        print("✅ PASSED: Dashboard stats-related endpoints are working.")

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

        # CORRECTED: Create a BusinessRole and a BusinessStaff instance
        manager_role = BusinessRole.objects.create(
            business=business, name="Test Manager Role"
        )
        dashboard_perm = Permission.objects.get(codename="access_business_dashboard")
        manager_role.permissions.add(dashboard_perm)
        manager_user.user_permissions.add(dashboard_perm)

        staff_profile = BusinessStaff.objects.create(
            business=business,
            user=manager_user,
            role=manager_role,
            status="accepted",
        )

        # 1. Verify manager CAN access
        self.client.force_authenticate(user=manager_user)
        url = reverse("my-business-overview")
        response_allowed = self.client.get(url)
        self.assertEqual(response_allowed.status_code, status.HTTP_200_OK)

        # 2. Remove manager from the business by deleting their staff profile
        staff_profile.delete()

        # 3. Verify manager CANNOT access anymore
        response_denied = self.client.get(url)
        self.assertEqual(response_denied.status_code, status.HTTP_403_FORBIDDEN)
        print("✅ PASSED: Removed manager correctly denied access to dashboard.")


class BusinessDiscountManagementTests(APITestCase):
    """
    Tests for a business owner creating and managing discounts and coupons.
    """

    def setUp(self):
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        _get_and_assign_permission(
            business_role, "manage_own_business_discounts", BusinessInfo
        )
        self.user.role = business_role
        self.user.save()
        self.user.user_permissions.add(*business_role.permissions.all())

        self.business = BusinessInfoFactory(owner=self.user)
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(
            businessId=self.business, category=self.category
        )
        self.option = ClassOptionFactory(classId=self.klass)
        self.client.force_authenticate(user=self.user)
        self.url = reverse("business-discount-list")

    def test_owner_can_create_percentage_coupon(self):
        """
        POST /api/business/discounts/ - Create a percentage-based coupon for a class.
        """
        print("\n--- Running: test_owner_can_create_percentage_coupon ---")
        data = {
            "name": "SUMMER20",
            "code": "SUMMER20",
            "discount_type": "percentage",
            "value": "20.00",
            "scope": "class",
            "target_class": self.klass.pk,
            "is_active": True,
            "usage_limit": 100,
        }
        response = self.client.post(self.url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            Discount.objects.filter(business=self.business, code="SUMMER20").exists()
        )
        print("✅ PASSED: Business owner can create a percentage coupon.")

    def test_owner_can_toggle_discount_active_status(self):
        """
        [NEW TEST] PATCH .../discounts/{pk}/toggle-active/ - An owner can toggle the active status.
        """
        print("\n--- Running: test_owner_can_toggle_discount_active_status ---")
        discount = Discount.objects.create(
            business=self.business,
            name="Toggle Me",
            code="TOGGLE",
            discount_type="fixed_amount",
            value=Decimal("5.00"),
            is_active=True,
            scope="class",
            target_class=self.klass,
        )
        url = reverse("business-discount-toggle-active", kwargs={"pk": discount.pk})

        # 1. Deactivate
        response_off = self.client.patch(url, {}, format="json")
        self.assertEqual(response_off.status_code, status.HTTP_200_OK)
        self.assertFalse(response_off.data["is_active"])
        discount.refresh_from_db()
        self.assertFalse(discount.is_active)

        # 2. Reactivate
        response_on = self.client.patch(url, {}, format="json")
        self.assertEqual(response_on.status_code, status.HTTP_200_OK)
        self.assertTrue(response_on.data["is_active"])
        discount.refresh_from_db()
        self.assertTrue(discount.is_active)
        print("✅ PASSED: Owner can successfully toggle a discount's active status.")

    def test_customer_can_validate_valid_coupon(self):
        """
        POST /api/business/discounts/validate-coupon/ - A valid coupon returns a success response.
        """
        print("\n--- Running: test_customer_can_validate_valid_coupon ---")
        Discount.objects.create(
            business=self.business,
            name="HOLIDAY10",
            code="HOLIDAY10",
            discount_type="fixed_amount",
            value=Decimal("10.00"),
            scope="class",
            target_class=self.klass,
            is_active=True,
        )
        url = reverse("business-discount-validate-coupon")
        data = {
            "code": "HOLIDAY10",
            "option_id": self.option.optionId,
            "base_amount": "50.00",
        }
        # Does not require auth
        self.client.force_authenticate(user=None)
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["calculated_discount_amount"], 10.00)
        print("✅ PASSED: A valid coupon was successfully validated.")

    def test_customer_cannot_validate_expired_coupon(self):
        """
        POST .../validate-coupon/ - An expired coupon returns a validation error.
        """
        print("\n--- Running: test_customer_cannot_validate_expired_coupon ---")
        # FIX: Set valid_from and valid_to to create a valid-in-the-past but now-expired coupon.
        Discount.objects.create(
            business=self.business,
            name="EXPIRED",
            code="EXPIRED",
            discount_type="percentage",
            value=Decimal("10.00"),
            scope="class",
            target_class=self.klass,
            is_active=True,
            valid_from=timezone.now() - timedelta(days=2),
            valid_to=timezone.now() - timedelta(days=1),
        )
        url = reverse("business-discount-validate-coupon")
        data = {
            "code": "EXPIRED",
            "option_id": self.option.optionId,
            "base_amount": "50.00",
        }
        self.client.force_authenticate(user=None)
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("expired", response.data["detail"])
        print("✅ PASSED: An expired coupon was correctly rejected.")
