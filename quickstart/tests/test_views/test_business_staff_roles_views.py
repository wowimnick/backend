# quickstart/tests/test_views/test_business_staff_roles_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from unittest.mock import patch

from quickstart.models import BusinessInfo, BusinessRole, BusinessStaff
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
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


class BusinessStaffAndRolesTests(APITestCase):
    def setUp(self):
        # --- Create Users ---
        self.owner = UserFactory()
        self.manager_user = UserFactory()
        self.invited_user = UserFactory(email="invitee@example.com")

        # --- Create Business ---
        self.business = BusinessInfoFactory(owner=self.owner)

        # --- Create Business-Specific Roles ---
        # FIX: Use get_or_create to prevent IntegrityError on subsequent test runs
        self.owner_role, _ = BusinessRole.objects.get_or_create(
            business=self.business, name="Business Owner"
        )
        self.manager_role, _ = BusinessRole.objects.get_or_create(
            business=self.business, name="Manager"
        )
        self.instructor_role, _ = BusinessRole.objects.get_or_create(
            business=self.business, name="Instructor"
        )

        # --- Assign Permissions to Roles ---
        _get_and_assign_permissions(
            self.manager_role,
            {
                BusinessInfo: ["manage_business_roles", "manage_business_staff"],
            },
        )
        # The instructor has no management permissions
        _get_and_assign_permissions(
            self.instructor_role,
            {
                BusinessInfo: ["manage_own_classes"],
            },
        )

        # --- Create Staff Members ---
        self.manager_staff_profile = BusinessStaff.objects.create(
            business=self.business,
            user=self.manager_user,
            role=self.manager_role,
            status="accepted",
            invited_email=self.manager_user.email,
        )

    # =================================================================
    # == Role Management Tests (BusinessRoleViewSet)
    # =================================================================
    def test_owner_can_list_roles(self):
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-role-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)  # Owner, Manager, Instructor

    def test_staff_with_permission_can_create_role(self):
        self.client.force_authenticate(user=self.manager_user)
        url = reverse("business-role-list")
        data = {"name": "Front Desk", "description": "Handles check-ins."}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            BusinessRole.objects.filter(
                business=self.business, name="Front Desk"
            ).exists()
        )

    def test_staff_without_permission_cannot_create_role(self):
        instructor_user = UserFactory()
        BusinessStaff.objects.create(
            business=self.business,
            user=instructor_user,
            role=self.instructor_role,
            status="accepted",
        )
        self.client.force_authenticate(user=instructor_user)
        url = reverse("business-role-list")
        data = {"name": "This Should Fail"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_role_with_assigned_staff(self):
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-role-detail", kwargs={"pk": self.manager_role.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("assigned to staff", response.data["detail"])

    def test_owner_can_edit_role_permissions(self):
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-role-detail", kwargs={"pk": self.instructor_role.pk})
        perm = Permission.objects.get(codename="view_own_business_bookings")
        data = {"permissions": [perm.id]}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.instructor_role.refresh_from_db()
        self.assertTrue(self.instructor_role.permissions.filter(pk=perm.id).exists())

    def test_staff_cannot_edit_own_role_permissions(self):
        """[SECURITY] A staff member cannot escalate their own privileges."""
        self.client.force_authenticate(user=self.manager_user)
        url = reverse("business-role-detail", kwargs={"pk": self.manager_role.pk})
        perm = Permission.objects.get(codename="view_business_revenue_analytics")
        current_perms = list(self.manager_role.permissions.values_list("id", flat=True))
        data = {"permissions": current_perms + [perm.id]}  # Attempt to add a perm
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            "change the permissions of your own role", response.data["detail"]
        )

    # =================================================================
    # == Staff Management Tests (BusinessStaffViewSet)
    # =================================================================
    @patch(
        "quickstart.views.business.business_staff_views.send_business_staff_invitation_email"
    )
    def test_owner_can_invite_staff(self, mock_send_email):
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-staff-list")
        data = {
            "invited_email": "new.instructor@example.com",
            "role": self.instructor_role.pk,
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            BusinessStaff.objects.filter(
                invited_email="new.instructor@example.com"
            ).exists()
        )
        mock_send_email.assert_called_once()

    def test_cannot_invite_already_existing_staff_member(self):
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-staff-list")
        data = {
            "invited_email": self.manager_user.email,
            "role": self.instructor_role.pk,
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already a member", str(response.data))

    def test_owner_can_remove_staff_member(self):
        self.client.force_authenticate(user=self.owner)
        url = reverse(
            "business-staff-detail", kwargs={"pk": self.manager_staff_profile.pk}
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            BusinessStaff.objects.filter(pk=self.manager_staff_profile.pk).exists()
        )

    # =================================================================
    # == Invitation Flow Tests
    # =================================================================
    def test_validate_invitation_token_works(self):
        invitation = BusinessStaff.objects.create(
            business=self.business,
            invited_email="pending@example.com",
            role=self.instructor_role,
        )
        url = reverse("validate-staff-invitation")
        response = self.client.get(url, {"token": invitation.invitation_token})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["business_name"], self.business.businessName)

    def test_user_can_accept_valid_invitation(self):
        invitation = BusinessStaff.objects.create(
            business=self.business,
            invited_email=self.invited_user.email,
            role=self.instructor_role,
        )
        self.client.force_authenticate(user=self.invited_user)
        url = reverse("accept-staff-invitation")
        data = {"token": invitation.invitation_token}
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "accepted")
        self.assertEqual(invitation.user, self.invited_user)
        self.assertIsNone(invitation.invitation_token)
        # Check that the response includes the updated user profile
        self.assertIn("user", response.data)
        self.assertIn(
            "quickstart.manage_own_classes", response.data["user"]["permissions"]
        )
