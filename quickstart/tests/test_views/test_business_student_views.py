# quickstart/tests/test_views/test_business_student_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import StudentNote, BusinessInfo, Role
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    BookingFactory,
    RoleFactory,
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


class BusinessStudentManagementTests(APITestCase):
    """
    Tests for business owners viewing student profiles and managing notes.
    """

    def setUp(self):
        # Create Roles and Permissions
        business_role = RoleFactory(name="Business Test Role")
        _get_and_assign_permission(
            business_role, "view_business_students", BusinessInfo
        )
        _get_and_assign_permission(business_role, "add_studentnote", BusinessInfo)
        # FIX: Add the missing 'view_studentnote' permission to the role.
        _get_and_assign_permission(business_role, "view_studentnote", BusinessInfo)

        # Create Users
        self.owner = UserFactory(role=business_role)
        self.student1 = UserFactory()
        self.student2 = UserFactory()
        self.unrelated_user = UserFactory()

        # Assign permissions directly to owner for test client
        self.owner.user_permissions.add(*business_role.permissions.all())

        # Create Business and Class structure
        self.business = BusinessInfoFactory(owner=self.owner)
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(
            businessId=self.business, category=self.category
        )

        # Create bookings to establish a student-business relationship
        BookingFactory(
            user=self.student1,
            schedule_instance__schedule__option__classId=self.klass,
        )
        BookingFactory(
            user=self.student2,
            schedule_instance__schedule__option__classId=self.klass,
        )

        # Create a booking with a different business
        other_category = ClassCategoryFactory()
        other_business_klass = ClassesMainFactory(category=other_category)
        BookingFactory(
            user=self.unrelated_user,
            schedule_instance__schedule__option__classId=other_business_klass,
        )

        self.client.force_authenticate(user=self.owner)

    def test_business_owner_can_list_their_students(self):
        """
        GET /api/business/students/ - An owner can list all users who have booked with them.
        """
        print("\n--- Running: test_business_owner_can_list_their_students ---")
        url = reverse("business-student-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)
        self.assertEqual(len(response.data["results"]), 2)

        student_emails = {s["email"] for s in response.data["results"]}
        self.assertIn(self.student1.email, student_emails)
        self.assertIn(self.student2.email, student_emails)
        self.assertNotIn(self.unrelated_user.email, student_emails)
        print("✅ PASSED: Business owner can view their list of students.")

    def test_business_owner_can_view_student_profile(self):
        """
        GET /api/business/students/{pk}/ - An owner can view a detailed profile of their student.
        """
        print("\n--- Running: test_business_owner_can_view_student_profile ---")
        url = reverse("business-student-detail", kwargs={"pk": self.student1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], self.student1.email)
        self.assertIn("booking_history", response.data)
        self.assertEqual(len(response.data["booking_history"]), 1)
        print("✅ PASSED: Business owner can view a student's detailed profile.")

    def test_business_owner_can_add_note_to_student(self):
        """
        POST /api/business/students/{pk}/add_note/ - An owner can add a private note.
        """
        print("\n--- Running: test_business_owner_can_add_note_to_student ---")
        url = reverse("business-student-add-note", kwargs={"pk": self.student1.pk})
        data = {
            "content": "Student is preparing for a competition. Needs encouragement."
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            StudentNote.objects.filter(
                user=self.student1, business=self.business
            ).exists()
        )
        note = StudentNote.objects.get(user=self.student1, business=self.business)
        self.assertEqual(note.content, data["content"])
        self.assertEqual(note.author, self.owner)
        print("✅ PASSED: Business owner successfully added a note to a student.")

    def test_note_appears_in_student_profile(self):
        """
        GET /api/business/students/{pk}/ - A previously added note should appear in the profile.
        """
        print("\n--- Running: test_note_appears_in_student_profile ---")
        # Add a note first
        StudentNote.objects.create(
            user=self.student1,
            business=self.business,
            author=self.owner,
            content="Excellent progress.",
        )

        url = reverse("business-student-detail", kwargs={"pk": self.student1.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # FIX: This assertion should now pass because the permission was added in setUp.
        self.assertIn("notes", response.data)
        self.assertEqual(len(response.data["notes"]), 1)
        self.assertEqual(response.data["notes"][0]["content"], "Excellent progress.")
        print("✅ PASSED: Student note is correctly displayed in the profile view.")
