# quickstart/tests/test_views/test_business_review_management_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from quickstart.models import Reviews, BusinessInfo, Role
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    BookingFactory,
    ReviewFactory,
    RoleFactory,
    ClassCategoryFactory,
)


class BusinessReviewManagementTests(APITestCase):
    def setUp(self):
        # Create Roles and Permissions
        business_role = RoleFactory(name="Business Owner")
        ContentType.objects.get_for_model(Reviews)
        perm_view = Permission.objects.get(codename="view_own_business_reviews")
        perm_respond = Permission.objects.get(codename="add_business_review_response")
        business_role.permissions.add(perm_view, perm_respond)

        self.owner = UserFactory(role=business_role)
        self.owner.user_permissions.add(perm_view, perm_respond)
        self.business = BusinessInfoFactory(owner=self.owner)
        self.category = ClassCategoryFactory()

        # Create classes and reviews for this business
        self.class1 = ClassesMainFactory(
            businessId=self.business, category=self.category
        )
        self.review1 = ReviewFactory(
            classId=self.class1, rating=5, comment="Excellent!"
        )
        self.review2 = ReviewFactory(
            classId=self.class1,
            rating=3,
            comment="It was okay.",
            status="under_review",
            createdAt=timezone.now() - timedelta(days=10),
        )

        # Create data for another business that should NOT be visible
        self.other_business = BusinessInfoFactory()
        self.other_class = ClassesMainFactory(
            businessId=self.other_business, category=self.category
        )
        self.other_review = ReviewFactory(classId=self.other_class)

        self.client.force_authenticate(user=self.owner)

    def test_owner_can_list_own_business_reviews(self):
        """
        GET /api/business/reviews/ - An owner can list all reviews for their business.
        """
        url = reverse("business-review-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        review_comments = {r["comment"] for r in response.data["results"]}
        self.assertIn("Excellent!", review_comments)
        self.assertIn("It was okay.", review_comments)
        self.assertNotIn(self.other_review.comment, review_comments)

    def test_owner_cannot_access_other_business_review_details(self):
        """
        GET /api/business/reviews/{pk}/ - An owner gets a 404 for another business's review.
        """
        url = reverse("business-review-detail", kwargs={"pk": self.other_review.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_review_analytics_endpoint(self):
        """
        GET /api/business/reviews/analytics/ - Test the review analytics data.
        """
        # Create a responded review
        self.review1.business_response = "Thank you!"
        self.review1.save()

        url = reverse("business-review-analytics")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("summary_metrics", data)
        self.assertEqual(data["summary_metrics"]["total_reviews_in_period"], 2)
        self.assertEqual(
            data["summary_metrics"]["average_rating_in_period"], 4.0
        )  # (5+3)/2
        self.assertEqual(
            data["summary_metrics"]["response_rate_in_period"], 50.0
        )  # 1 of 2 responded
        self.assertEqual(data["summary_metrics"]["reviews_under_review"], 1)

        self.assertIn("rating_distribution", data)
        self.assertEqual(data["rating_distribution"][4]["count"], 1)  # 5 star
        self.assertEqual(data["rating_distribution"][2]["count"], 1)  # 3 star

    def test_owner_can_report_review(self):
        """
        POST /api/business/reviews/{pk}/report/ - An owner can report a review.
        """
        url = reverse("business-review-report", kwargs={"pk": self.review1.pk})
        data = {"report_reason": "This review contains inappropriate content."}
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.review1.refresh_from_db()
        self.assertTrue(self.review1.reported)
        self.assertEqual(self.review1.report_reason, data["report_reason"])
        self.assertEqual(self.review1.status, "under_review")
        self.assertIsNotNone(self.review1.reported_at)

    def test_cannot_report_review_twice(self):
        """
        POST .../report/ - A review that is already reported cannot be reported again.
        """
        # Report the review first
        self.review1.reported = True
        self.review1.save()

        url = reverse("business-review-report", kwargs={"pk": self.review1.pk})
        data = {"report_reason": "Trying to report again."}
        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been reported", response.data["message"])
