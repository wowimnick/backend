# quickstart/tests/test_views/test_business_student_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import StudentNote, BusinessInfo, Role, Contact
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
        # This will also trigger the creation of a Contact record for student1 and student2
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
        # FIX: The URL PK must be the Contact's PK, not the User's.
        contact = Contact.objects.get(user=self.student1, business=self.business)
        url = reverse("business-student-detail", kwargs={"pk": contact.pk})
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
        # FIX: The URL PK must be the Contact's PK.
        contact = Contact.objects.get(user=self.student1, business=self.business)
        url = reverse("business-student-add-note", kwargs={"pk": contact.pk})
        data = {
            "content": "Student is preparing for a competition. Needs encouragement."
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # FIX: The assertion must check for a note linked to the Contact object.
        contact_type = ContentType.objects.get_for_model(Contact)
        self.assertTrue(
            StudentNote.objects.filter(
                content_type=contact_type, object_id=contact.pk, business=self.business
            ).exists()
        )
        note = StudentNote.objects.get(
            content_type=contact_type, object_id=contact.pk, business=self.business
        )
        self.assertEqual(note.content, data["content"])
        self.assertEqual(note.author, self.owner)
        print("✅ PASSED: Business owner successfully added a note to a student.")

    def test_note_appears_in_student_profile(self):
        """
        GET /api/business/students/{pk}/ - A previously added note should appear in the profile.
        """
        print("\n--- Running: test_note_appears_in_student_profile ---")
        # FIX: Create the note by linking it to the Contact object, not the user.
        contact = Contact.objects.get(user=self.student1, business=self.business)
        StudentNote.objects.create(
            content_object=contact,
            business=self.business,
            author=self.owner,
            content="Excellent progress.",
        )

        # FIX: Use the Contact's PK in the URL.
        url = reverse("business-student-detail", kwargs={"pk": contact.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("notes", response.data)
        self.assertEqual(len(response.data["notes"]), 1)
        self.assertEqual(response.data["notes"][0]["content"], "Excellent progress.")
        print("✅ PASSED: Student note is correctly displayed in the profile view.")
