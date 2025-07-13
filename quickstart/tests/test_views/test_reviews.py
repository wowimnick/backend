# quickstart/tests/test_views/test_reviews.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

# FIX: Import Pillow and io to create a real image in memory
from PIL import Image
import io

from quickstart.models import Reviews, Booking, BusinessInfo, Role
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    BookingFactory,
    ReviewFactory,
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


class ReviewManagementTests(APITestCase):
    """
    Tests the full lifecycle of a review: submission by a student
    and response/management by a business owner.
    """

    def setUp(self):
        # Create Roles and Permissions
        student_role = RoleFactory(name="Student", is_default=True)
        business_role = RoleFactory(name="Business Owner")
        _get_and_assign_permission(
            business_role, "view_own_business_reviews", BusinessInfo
        )
        _get_and_assign_permission(
            business_role, "add_business_review_response", Reviews
        )

        # Create Users
        self.student = UserFactory(role=student_role)
        self.other_student = UserFactory(role=student_role)
        self.owner = UserFactory(role=business_role)

        # Assign permissions directly to users for test client
        self.owner.user_permissions.add(*business_role.permissions.all())

        # Create Business and Class structure
        self.business = BusinessInfoFactory(owner=self.owner)
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(
            businessId=self.business, category=self.category
        )

        # Create a COMPLETED booking, which is eligible for review
        self.completed_booking = BookingFactory(
            user=self.student,
            schedule_instance__schedule__option__classId=self.klass,
            status="completed",
        )

        # Create an UPCOMING booking, which is NOT eligible for review
        self.upcoming_booking = BookingFactory(
            user=self.student,
            schedule_instance__schedule__option__classId=self.klass,
            status="confirmed",
        )

        # Create a completed booking for another student to test permission boundaries
        self.other_student_booking = BookingFactory(
            user=self.other_student,
            schedule_instance__schedule__option__classId=self.klass,
            status="completed",
        )

    def test_student_can_submit_review_for_completed_booking(self):
        """
        POST /api/reviews/submit/ - A student can submit a review for a completed class.
        """
        print("\n--- Running: test_student_can_submit_review_for_completed_booking ---")
        self.client.force_authenticate(user=self.student)
        url = reverse("submit-review")

        # FIX: Create a real, valid image in memory using Pillow.
        image = Image.new("RGB", (100, 100))
        image_io = io.BytesIO()
        image.save(image_io, "JPEG")
        image_io.seek(0)
        image_file = SimpleUploadedFile(
            "review_pic.jpg", image_io.read(), content_type="image/jpeg"
        )

        data = {
            "booking_id": self.completed_booking.id,
            "rating": 5,
            "comment": "This was an absolutely fantastic class! Highly recommended.",
            "image": image_file,
        }

        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Reviews.objects.filter(booking=self.completed_booking).exists())
        review = Reviews.objects.get(booking=self.completed_booking)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.userId, self.student)
        self.assertTrue(review.image.name.startswith("review_images/"))
        print("✅ PASSED: Student successfully submitted a review.")

    def test_student_cannot_submit_review_for_upcoming_booking(self):
        """
        POST /api/reviews/submit/ - A student cannot review a class that is not completed.
        """
        print(
            "\n--- Running: test_student_cannot_submit_review_for_upcoming_booking ---"
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("submit-review")
        data = {
            "booking_id": self.upcoming_booking.id,
            "rating": 4,
            "comment": "Trying to review this early.",
        }

        # FIX: Use format="multipart" to match the view's parser configuration.
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("You can only review completed bookings", str(response.data))
        self.assertFalse(Reviews.objects.filter(booking=self.upcoming_booking).exists())
        print(
            "✅ PASSED: Student correctly prevented from reviewing an upcoming class."
        )

    def test_student_cannot_submit_duplicate_review(self):
        """
        POST /api/reviews/submit/ - A student cannot submit two reviews for the same booking.
        """
        print("\n--- Running: test_student_cannot_submit_duplicate_review ---")
        # First, create a valid review
        ReviewFactory(booking=self.completed_booking, userId=self.student)

        self.client.force_authenticate(user=self.student)
        url = reverse("submit-review")
        data = {
            "booking_id": self.completed_booking.id,
            "rating": 3,
            "comment": "This is a second attempt to review.",
        }

        # FIX: Use format="multipart" to match the view's parser configuration.
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been submitted", str(response.data))
        self.assertEqual(
            Reviews.objects.filter(booking=self.completed_booking).count(), 1
        )
        print("✅ PASSED: Student prevented from submitting a duplicate review.")

    def test_student_cannot_review_another_users_booking(self):
        """
        [EDGE CASE] POST /api/reviews/submit/ - A student cannot review a booking they don't own.
        """
        print("\n--- Running: test_student_cannot_review_another_users_booking ---")
        self.client.force_authenticate(user=self.student)
        url = reverse("submit-review")
        data = {
            "booking_id": self.other_student_booking.id,  # Try to review other student's booking
            "rating": 5,
            "comment": "I am trying to review a class I did not take.",
        }

        response = self.client.post(url, data, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("You can only review your own", str(response.data))
        print("✅ PASSED: Student correctly blocked from reviewing another's booking.")

    def test_business_owner_can_respond_to_review(self):
        """
        POST /api/business/reviews/{pk}/respond/ - An owner can respond to a review.
        """
        print("\n--- Running: test_business_owner_can_respond_to_review ---")
        # Create a review for the business owner to respond to
        review = ReviewFactory(
            classId=self.klass, userId=self.student, booking=self.completed_booking
        )

        self.client.force_authenticate(user=self.owner)
        url = reverse("business-review-respond", kwargs={"pk": review.pk})
        data = {"business_response": "Thank you so much for your feedback!"}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        review.refresh_from_db()
        self.assertEqual(review.business_response, data["business_response"])
        self.assertIsNotNone(review.responded_at)
        print("✅ PASSED: Business owner successfully responded to a review.")

    def test_business_owner_cannot_respond_to_other_business_review(self):
        """
        [EDGE CASE] POST .../respond/ - An owner cannot respond to a review for another business.
        """
        print(
            "\n--- Running: test_business_owner_cannot_respond_to_other_business_review ---"
        )
        # Create a review for a completely different business
        other_business_review = ReviewFactory()

        self.client.force_authenticate(user=self.owner)
        url = reverse(
            "business-review-respond", kwargs={"pk": other_business_review.pk}
        )
        data = {"business_response": "This response should not be saved."}

        response = self.client.post(url, data, format="json")

        # The view's queryset will fail to find the review, resulting in a 404.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        print("✅ PASSED: Owner correctly receives 404 for review on another business.")
