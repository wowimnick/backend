# quickstart/tests/test_views/test_admin_user_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission
from django.test import override_settings  # <<< MODIFIED: Import override_settings
from django.conf import settings  # <<< MODIFIED: Import settings

from quickstart.models import CustomUser, Role, AuditLog
from quickstart.tests.factories import UserFactory, RoleFactory


def _get_and_assign_permissions(role, permissions_map):
    """Helper to assign multiple permissions to a role."""
    for model_class, codenames in permissions_map.items():
        content_type = ContentType.objects.get_for_model(model_class)
        for codename in codenames:
            permission, _ = Permission.objects.get_or_create(
                codename=codename,
                content_type=content_type,
            )
            role.permissions.add(permission)


# --- FIX: Disable Silk middleware for all tests in this class ---
@override_settings(
    MIDDLEWARE=[mw for mw in settings.MIDDLEWARE if "silk.middleware" not in mw]
)
class AdminUserManagementTests(APITestCase):
    """
    Tests for admin-level user management, focusing on permissions and hierarchy.
    """

    def setUp(self):
        # Create Roles with different hierarchy levels
        self.super_admin_role = RoleFactory(name="Super Admin", hierarchy_level=100)
        self.manager_role = RoleFactory(name="Manager", hierarchy_level=50)
        self.student_role = RoleFactory(name="Student", hierarchy_level=10)

        # Assign permissions to the roles
        _get_and_assign_permissions(
            self.super_admin_role,
            {
                CustomUser: [
                    "access_user_admin",
                    "view_customuser",
                    "change_customuser",
                    "delete_customuser",
                    "change_user_role",
                    "lock_user",
                ]
            },
        )
        _get_and_assign_permissions(
            self.manager_role,
            {
                CustomUser: [
                    "access_user_admin",
                    "view_customuser",
                    "change_customuser",
                    "lock_user",
                ]
            },
        )

        # Create Users
        self.super_admin = UserFactory(role=self.super_admin_role)
        self.manager = UserFactory(role=self.manager_role)
        self.student = UserFactory(role=self.student_role)

        # Directly assign permissions to users for test client checks
        self.super_admin.user_permissions.add(*self.super_admin_role.permissions.all())
        self.manager.user_permissions.add(*self.manager_role.permissions.all())

    def test_admin_can_list_all_users(self):
        """
        GET /api/platform-admin/users/ - An admin can list all users.
        """
        print("\n--- Running: test_admin_can_list_all_users ---")
        self.client.force_authenticate(user=self.manager)
        url = reverse("admin-users-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Includes the 3 users created in setUp
        self.assertEqual(len(response.data["results"]), 3)
        print("✅ PASSED: Admin can successfully list users.")

    def test_manager_can_update_student_profile(self):
        """
        PATCH /api/platform-admin/users/{pk}/ - Manager can update a user with a lower role.
        """
        print("\n--- Running: test_manager_can_update_student_profile ---")
        self.client.force_authenticate(user=self.manager)
        url = reverse("admin-users-detail", kwargs={"pk": self.student.pk})
        data = {"first_name": "UpdatedByManager"}

        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.first_name, "UpdatedByManager")
        print("✅ PASSED: Manager updated a student's profile.")

    def test_manager_cannot_update_super_admin_profile(self):
        """
        PATCH /api/platform-admin/users/{pk}/ - Manager cannot update a user with a higher role.
        """
        print("\n--- Running: test_manager_cannot_update_super_admin_profile ---")
        self.client.force_authenticate(user=self.manager)
        url = reverse("admin-users-detail", kwargs={"pk": self.super_admin.pk})
        data = {"first_name": "ShouldNotUpdate"}

        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("hierarchy restrictions", response.data["detail"])
        print("✅ PASSED: Manager correctly blocked from updating a super admin.")

    def test_manager_cannot_update_equal_level_manager(self):
        """
        [EDGE CASE] PATCH .../users/{pk}/ - Manager cannot update another user with the same role level.
        """
        print("\n--- Running: test_manager_cannot_update_equal_level_manager ---")
        # Create another manager with the same role and hierarchy level
        other_manager = UserFactory(role=self.manager_role)
        other_manager.user_permissions.add(*self.manager_role.permissions.all())

        self.client.force_authenticate(user=self.manager)
        url = reverse("admin-users-detail", kwargs={"pk": other_manager.pk})
        data = {"first_name": "ShouldAlsoNotUpdate"}

        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("hierarchy restrictions", response.data["detail"])
        print(
            "✅ PASSED: Manager correctly blocked from updating an equal-level manager."
        )

    def test_super_admin_can_change_user_role(self):
        """
        PATCH /api/platform-admin/users/{pk}/ - Super Admin can change another user's role.
        """
        print("\n--- Running: test_super_admin_can_change_user_role ---")
        self.client.force_authenticate(user=self.super_admin)
        url = reverse("admin-users-detail", kwargs={"pk": self.student.pk})
        data = {"role": self.manager_role.pk}

        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.student.refresh_from_db()
        self.assertEqual(self.student.role, self.manager_role)
        # Check if an audit log was created for the role change
        self.assertTrue(
            AuditLog.objects.filter(
                target_user=self.student, action="role_change"
            ).exists()
        )
        print("✅ PASSED: Super Admin successfully changed a user's role.")

    def test_manager_cannot_lock_super_admin_account(self):
        """
        POST /api/platform-admin/users/{pk}/lock_account/ - Manager cannot lock a higher role.
        """
        print("\n--- Running: test_manager_cannot_lock_super_admin_account ---")
        self.client.force_authenticate(user=self.manager)
        url = reverse("admin-users-lock-account", kwargs={"pk": self.super_admin.pk})

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.super_admin.refresh_from_db()
        self.assertTrue(self.super_admin.is_active)
        print("✅ PASSED: Manager blocked from locking a super admin's account.")

    def test_super_admin_can_delete_user(self):
        """
        DELETE /api/platform-admin/users/{pk}/ - Super Admin can delete a user.
        """
        print("\n--- Running: test_super_admin_can_delete_user ---")
        user_to_delete = UserFactory()
        user_id = user_to_delete.pk

        self.client.force_authenticate(user=self.super_admin)
        url = reverse("admin-users-detail", kwargs={"pk": user_id})
        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(CustomUser.objects.filter(pk=user_id).exists())
        print("✅ PASSED: Super Admin successfully deleted a user.")

    def test_user_list_is_performant_and_avoids_n1_queries(self):
        """
        [PERFORMANCE] GET .../users/ - Ensures the user list endpoint is efficient.
        """
        print("\n--- Running: test_user_list_is_performant_and_avoids_n1_queries ---")

        # 1. Setup: Create a significant number of users to expose N+1 issues.
        student_role = Role.objects.get(name="Student")
        UserFactory.create_batch(17, role=student_role)
        # FIX: The total count is 3 from setUp + 17 from the batch = 20
        self.assertEqual(CustomUser.objects.count(), 20)

        self.client.force_authenticate(user=self.super_admin)
        url = reverse("admin-users-list")

        # --- FIX: Use a context manager to override settings for this specific test ---
        with self.settings(SILKY_META=False):
            # The query count is 5:
            # 1-2. Permission checks (user perms and group perms)
            # 3. COUNT query for pagination.
            # 4. SELECT query for the main user list (with role and bookings_count).
            # 5. PREFETCH query for the owned_businesses.
            with self.assertNumQueries(5):
                response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 10)  # Default page size

        print("✅ PASSED: User list endpoint is performant (avoids N+1 queries).")

    def test_super_admin_cannot_delete_self(self):
        """
        [EDGE CASE] DELETE .../users/{pk}/ - A user cannot delete their own account via the admin API.
        """
        print("\n--- Running: test_super_admin_cannot_delete_self ---")
        self.client.force_authenticate(user=self.super_admin)
        url = reverse("admin-users-detail", kwargs={"pk": self.super_admin.pk})

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("You cannot delete your own account", response.data["detail"])
        self.assertTrue(CustomUser.objects.filter(pk=self.super_admin.pk).exists())
        print("✅ PASSED: Admin prevented from deleting their own account.")
