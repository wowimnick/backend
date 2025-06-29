# quickstart/tests/test_views/test_admin_business_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import BusinessInfo, Role, VerificationRequest
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
    VerificationRequestFactory,
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


class AdminBusinessManagementTests(APITestCase):
    def setUp(self):
        # Create Roles
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        self.business_owner_role = RoleFactory(
            name="Business Owner", hierarchy_level=50
        )

        # Assign Permissions
        _get_and_assign_permissions(
            self.admin_role,
            {
                BusinessInfo: ["access_business_admin", "view_businessinfo"],
                VerificationRequest: [
                    "view_all_verificationrequests",
                    "process_verificationrequest",
                ],
            },
        )

        # Create Users
        self.admin_user = UserFactory(role=self.admin_role)
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())

        # Create Business and Verification Request
        self.business = BusinessInfoFactory(
            owner__role=self.business_owner_role,
            verificationStatus="pending",
            isActive=False,
        )
        self.verification_request = VerificationRequestFactory(
            business=self.business, user=self.business.owner, status="pending"
        )

    def test_admin_can_list_businesses(self):
        """
        GET /api/platform-admin/businesses/ - Admin can list all businesses.
        """
        print("\n--- Running: test_admin_can_list_businesses ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse("admin-businesses-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(
            response.data["results"][0]["businessName"], self.business.businessName
        )
        print("✅ PASSED: Admin can list businesses.")

    def test_admin_can_approve_verification_request(self):
        """
        POST .../process_verification/ - Approving a request updates business status.
        """
        print("\n--- Running: test_admin_can_approve_verification_request ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse(
            "admin-verification-process-verification",
            kwargs={"pk": self.verification_request.pk},
        )
        data = {"status": "approved", "notes": "All documents look good."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Refresh objects from DB
        self.business.refresh_from_db()
        self.business.owner.refresh_from_db()
        self.verification_request.refresh_from_db()

        # Assert changes
        self.assertEqual(self.business.verificationStatus, "verified")
        self.assertTrue(self.business.isActive)
        self.assertEqual(self.verification_request.status, "verified")
        self.assertEqual(
            self.business.owner.role.name, "Business Owner"
        )  # Assuming it was set on creation
        print("✅ PASSED: Verification approval workflow successful.")

    def test_admin_can_reject_verification_request(self):
        """
        POST .../process_verification/ - Rejecting a request updates business status.
        """
        print("\n--- Running: test_admin_can_reject_verification_request ---")
        self.client.force_authenticate(user=self.admin_user)
        url = reverse(
            "admin-verification-process-verification",
            kwargs={"pk": self.verification_request.pk},
        )
        data = {
            "status": "rejected",
            "rejection_reason": "Business license is expired.",
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # Refresh objects from DB
        self.business.refresh_from_db()
        self.verification_request.refresh_from_db()

        # Assert changes
        self.assertEqual(self.business.verificationStatus, "rejected")
        self.assertFalse(self.business.isActive)
        self.assertEqual(self.verification_request.status, "rejected")
        print("✅ PASSED: Verification rejection workflow successful.")
