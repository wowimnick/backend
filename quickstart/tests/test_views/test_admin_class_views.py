# quickstart/tests/test_views/test_admin_class_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import ClassesMain, Reviews, Role, ClassCategory
from quickstart.tests.factories import (
    UserFactory,
    ClassesMainFactory,
    ReviewFactory,
    RoleFactory,
    ClassCategoryFactory,
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


class AdminClassReviewManagementTests(APITestCase):
    def setUp(self):
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        _get_and_assign_permissions(
            self.admin_role,
            {
                ClassesMain: [
                    "access_class_admin",
                    "view_classesmain",
                    "change_class_status",
                ],
                Reviews: ["access_review_admin", "view_reviews", "change_reviews"],
                ClassCategory: [  # Add permissions for category management
                    "access_category_admin",
                    "delete_classcategory",
                ],
            },
        )
        self.admin_user = UserFactory(role=self.admin_role)
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())

        self.klass1 = ClassesMainFactory(title="Active Class", status="active")
        self.klass2 = ClassesMainFactory(title="Inactive Class", status="inactive")
        self.review1 = ReviewFactory(classId=self.klass1, status="approved")
        self.review2 = ReviewFactory(classId=self.klass2, status="under_review")

        self.client.force_authenticate(user=self.admin_user)

    def test_admin_can_list_all_classes(self):
        """
        GET /api/platform-admin/classes/ - Admin can list classes from all businesses.
        """
        print("\n--- Running: test_admin_can_list_all_classes ---")
        url = reverse("admin-classes-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        print("✅ PASSED: Admin can list all classes.")

    def test_admin_can_change_class_status(self):
        """
        PATCH .../update_class_status/ - Admin can suspend a class.
        """
        print("\n--- Running: test_admin_can_change_class_status ---")
        url = reverse(
            "admin-classes-update-class-status", kwargs={"pk": self.klass1.pk}
        )
        data = {"status": "suspended"}

        response = self.client.patch(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.klass1.refresh_from_db()
        self.assertEqual(self.klass1.status, "suspended")
        print("✅ PASSED: Admin can change class status.")

    def test_admin_can_list_all_reviews(self):
        """
        GET /api/platform-admin/reviews/ - Admin can list all reviews.
        """
        print("\n--- Running: test_admin_can_list_all_reviews ---")
        url = reverse("admin-reviews-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        print("✅ PASSED: Admin can list all reviews.")

    def test_admin_can_change_review_status(self):
        """
        POST .../update_status/ - Admin can change a review's status.
        """
        print("\n--- Running: test_admin_can_change_review_status ---")
        url = reverse("admin-reviews-update-status", kwargs={"pk": self.review2.pk})
        data = {"status": "hidden"}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.review2.refresh_from_db()
        self.assertEqual(self.review2.status, "hidden")
        print("✅ PASSED: Admin can moderate review status.")

    def test_cannot_delete_category_with_assigned_classes(self):
        """
        [EDGE CASE] DELETE .../categories/{pk}/ - Admin cannot delete a category with classes assigned.
        """
        print("\n--- Running: test_cannot_delete_category_with_assigned_classes ---")
        # klass1 is already assigned to a category via the factory
        category_with_class = self.klass1.category
        url = reverse("admin-categories-detail", kwargs={"pk": category_with_class.pk})

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reassignment_required", response.data["code"])
        self.assertTrue(
            ClassCategory.objects.filter(pk=category_with_class.pk).exists()
        )
        print("✅ PASSED: Correctly blocked deletion of a category in use.")

    def test_can_delete_category_with_reassignment(self):
        """
        [EDGE CASE] POST .../delete-with-reassignment/ - Admin can reassign classes and delete a category.
        """
        print("\n--- Running: test_can_delete_category_with_reassignment ---")
        category_to_delete = self.klass1.category
        new_category = ClassCategoryFactory(name="New Home for Classes")

        url = reverse(
            "admin-categories-delete-with-reassignment",
            kwargs={"pk": category_to_delete.pk},
        )
        data = {"new_id": new_category.pk}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.klass1.refresh_from_db()
        self.assertEqual(self.klass1.category, new_category)
        self.assertIsNone(
            self.klass1.subcategory
        )  # Subcategory should be cleared on reassignment
        self.assertFalse(
            ClassCategory.objects.filter(pk=category_to_delete.pk).exists()
        )
        print("✅ PASSED: Category reassignment and deletion successful.")
