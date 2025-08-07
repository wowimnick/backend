from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import date, timedelta, time
from unittest.mock import patch
from decimal import Decimal
from django.test import override_settings
from django.conf import settings

from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    ReviewFactory,
    ClassOptionFactory,
    ScheduleInstanceFactory,
    BookingFactory,
    ClassCategoryFactory,
)
from quickstart.models import BusinessInfo, Favorites


@override_settings(
    MIDDLEWARE=[mw for mw in settings.MIDDLEWARE if "silk.middleware" not in mw]
)
class PublicBusinessInfoViewSetTest(APITestCase):
    """
    Tests for the public-facing business information endpoint.
    Covers listing, retrieval, and the secure contact_details action.
    """

    def setUp(self):
        # Create businesses with different statuses
        self.active_verified_business = BusinessInfoFactory(
            businessName="Active Verified School",
            isActive=True,
            verificationStatus="verified",
            contact_privacy="public",
        )
        self.active_unverified_business = BusinessInfoFactory(
            businessName="Active Unverified School",
            isActive=True,
            verificationStatus="pending",
        )
        self.inactive_business = BusinessInfoFactory(
            businessName="Inactive School",
            isActive=False,
            verificationStatus="verified",
        )
        self.private_contact_business = BusinessInfoFactory(
            businessName="Private Contact School",
            isActive=True,
            verificationStatus="verified",
            contact_privacy="on_booking",
        )

    def test_list_only_active_and_verified_businesses(self):
        """
        GET /api/businesses/ - Should only return businesses that are both active and verified.
        """
        url = reverse("public-business-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Because pagination is now active, the response is a dictionary.
        self.assertIn("results", response.data)
        self.assertEqual(
            len(response.data["results"]), 2
        )  # active_verified_business and private_contact_business

        business_names = {b["businessName"] for b in response.data["results"]}
        self.assertIn("Active Verified School", business_names)
        self.assertIn("Private Contact School", business_names)
        self.assertNotIn("Active Unverified School", business_names)
        self.assertNotIn("Inactive School", business_names)

    def test_retrieve_active_verified_business(self):
        """
        GET /api/businesses/{pk}/ - Should successfully retrieve an active, verified business.
        """
        url = reverse(
            "public-business-detail", kwargs={"pk": self.active_verified_business.pk}
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["businessName"], self.active_verified_business.businessName
        )

    def test_cannot_retrieve_inactive_business(self):
        """
        GET /api/businesses/{pk}/ - Should return 404 for an inactive business.
        """
        url = reverse(
            "public-business-detail", kwargs={"pk": self.inactive_business.pk}
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_contact_details_hidden_by_privacy_setting(self):
        """
        GET /api/businesses/{pk}/ - Contact details should be omitted based on privacy setting.
        """
        # Test the business with public contacts
        public_url = reverse(
            "public-business-detail", kwargs={"pk": self.active_verified_business.pk}
        )
        public_response = self.client.get(public_url)
        self.assertIn("studentContactEmail", public_response.data)
        self.assertIn("studentContactPhone", public_response.data)

        # Test the business with private contacts
        private_url = reverse(
            "public-business-detail", kwargs={"pk": self.private_contact_business.pk}
        )
        private_response = self.client.get(private_url)
        self.assertNotIn("studentContactEmail", private_response.data)
        self.assertNotIn("studentContactPhone", private_response.data)

    def test_contact_details_action_permission_denied_for_unauthenticated(self):
        """
        GET .../contact_details/ - Should deny access to unauthenticated users.
        """
        url = reverse(
            "public-business-contact-details",
            kwargs={"pk": self.private_contact_business.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_contact_details_action_permission_denied_for_user_without_booking(self):
        """
        GET .../contact_details/ - Should deny access to user with no relevant booking.
        """
        user = UserFactory()
        self.client.force_authenticate(user=user)

        url = reverse(
            "public-business-contact-details",
            kwargs={"pk": self.private_contact_business.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("You must have a booking", response.data["detail"])

    def test_contact_details_action_allowed_for_user_with_booking(self):
        """
        GET .../contact_details/ - Should reveal details to a user with a confirmed booking.
        """
        user = UserFactory()
        klass = ClassesMainFactory(businessId=self.private_contact_business)
        BookingFactory(
            user=user,
            schedule_instance__schedule__option__classId=klass,
            status="confirmed",
        )

        self.client.force_authenticate(user=user)
        url = reverse(
            "public-business-contact-details",
            kwargs={"pk": self.private_contact_business.pk},
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["studentContactEmail"],
            self.private_contact_business.studentContactEmail,
        )
        self.assertEqual(
            response.data["studentContactPhone"],
            self.private_contact_business.studentContactPhone,
        )

    def test_business_list_is_performant_and_avoids_n1_queries(self):
        """
        [PERFORMANCE] GET /api/businesses/ - Ensures public business list is efficient.
        """
        print(
            "\n--- Running: test_business_list_is_performant_and_avoids_n1_queries ---"
        )
        BusinessInfoFactory.create_batch(
            16, isActive=True, verificationStatus="verified"
        )
        self.assertEqual(
            BusinessInfo.objects.filter(
                isActive=True, verificationStatus="verified"
            ).count(),
            18,
        )

        url = reverse("public-business-list")

        with self.settings(SILKY_META=False):
            with self.assertNumQueries(2):
                response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 10)

        print(
            "✅ PASSED: Public business list endpoint is performant (avoids N+1 queries)."
        )


class PublicClassViewSetTest(APITestCase):
    """
    Tests for the public-facing class endpoint.
    Covers searching, filtering, favoriting, and relevance scoring logic.
    """

    def setUp(self):
        self.user = UserFactory()
        self.category = ClassCategoryFactory()
        self.active_class = ClassesMainFactory(
            title="Active Yoga Class",
            description="A class for testing.",
            coordinates="45.4215,-75.6972",  # Ottawa, ON
            category=self.category,
        )
        ReviewFactory.create_batch(5, classId=self.active_class, rating=5)

        self.inactive_class = ClassesMainFactory(
            title="Inactive Class", status="inactive", category=self.category
        )
        self.unverified_biz_class = ClassesMainFactory(
            title="Unverified Business Class",
            businessId__verificationStatus="pending",
            category=self.category,
        )

    def test_list_only_active_and_verified_classes(self):
        """
        GET /api/classes/ - Should only list classes that are active from verified businesses.
        """
        url = reverse("public-class-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertIn("results", response.data)
        self.assertIsInstance(
            response.data["results"],
            list,
            "The 'results' key in the response should contain a list.",
        )

        class_titles = [c["title"] for c in response.data["results"]]

        self.assertIn(self.active_class.title, class_titles)
        self.assertNotIn(self.inactive_class.title, class_titles)
        self.assertNotIn(self.unverified_biz_class.title, class_titles)

    def test_is_favorited_field_for_authenticated_user(self):
        """
        GET /api/classes/ - The 'is_favorited' field should be correct for a logged-in user.
        """
        # Add the class to the user's favorites
        self.active_class.favorited_by.add(self.user)

        self.client.force_authenticate(user=self.user)
        url = reverse("public-class-detail", kwargs={"pk": self.active_class.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_favorited"])

    def test_is_favorited_field_for_unauthenticated_user(self):
        """
        GET /api/classes/ - The 'is_favorited' field should be false for anonymous users.
        """
        url = reverse("public-class-detail", kwargs={"pk": self.active_class.pk})
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_favorited"])

    def test_toggle_favorite_action(self):
        """
        POST /api/classes/{pk}/toggle-favorite/ - Should add/remove a class from favorites.
        """
        self.client.force_authenticate(user=self.user)
        url = reverse(
            "public-class-toggle-favorite", kwargs={"pk": self.active_class.pk}
        )

        # 1. Favorite the class
        response_add = self.client.post(url)
        self.assertEqual(response_add.status_code, status.HTTP_200_OK)
        self.assertTrue(response_add.data["is_favorited"])
        self.assertTrue(
            Favorites.objects.filter(
                userId=self.user, classId=self.active_class
            ).exists()
        )

        # 2. Unfavorite the class
        response_remove = self.client.post(url)
        self.assertEqual(response_remove.status_code, status.HTTP_200_OK)
        self.assertFalse(response_remove.data["is_favorited"])
        self.assertFalse(
            Favorites.objects.filter(
                userId=self.user, classId=self.active_class
            ).exists()
        )

    # FIX: Removed patch and updated test to align with backend changes (no more geocoding).
    def test_search_by_location_coordinates(self):
        """
        GET /api/classes/search/?lat=...&lng=... - Should use coordinates and filter by distance.
        """
        # Create a class far away that should be excluded by radius
        ClassesMainFactory(
            title="Far Away Class",
            coordinates="34.0522,-118.2437",
            category=self.category,
        )  # Los Angeles

        url = reverse("public-class-search")
        # Search for Toronto (43.6532, -79.3832), radius of 500km should include Ottawa but not LA
        query_params = {"lat": "43.6532", "lng": "-79.3832", "radius": "500"}

        response = self.client.get(url, query_params)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["title"], "Active Yoga Class")

    # FIX: Rewrote test to check for graceful handling of invalid coordinate parameters.
    def test_search_with_invalid_coordinates(self):
        """
        [EDGE CASE] GET .../search/?lat=... - Should gracefully handle invalid coordinates.
        """
        print("\n--- Running: test_search_with_invalid_coordinates ---")
        # Create multiple classes
        ClassesMainFactory(title="Class A", category=self.category)
        ClassesMainFactory(title="Class B", category=self.category)

        url = reverse("public-class-search")
        query_params = {"lat": "asdf", "lng": "ghjk", "radius": "50"}

        response = self.client.get(url, query_params)

        # The search should still succeed, but it will not filter by location.
        # It should return all available classes.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len(response.data["results"]), 3
        )  # self.active_class, Class A, Class B
        print(
            "✅ PASSED: Search with invalid coordinates returns all results, not an error."
        )

    def test_search_with_date_and_participants_filter(self):
        """
        GET /api/classes/search/?date=...&participants=... - Should filter by instance availability.
        """
        # Setup: create specific schedule instances
        tomorrow = timezone.now().date() + timedelta(days=1)

        # This instance is available tomorrow for up to 10 people
        ScheduleInstanceFactory(
            schedule__option__classId=self.active_class,
            date=tomorrow,
            max_participants=10,
        )
        # This instance is full
        full_instance = ScheduleInstanceFactory(
            schedule__option__classId=self.active_class,
            date=tomorrow,
            max_participants=2,
        )
        BookingFactory(schedule_instance=full_instance, participants=2)

        url = reverse("public-class-search")

        # Case 1: Search for tomorrow with 5 participants, should find the class
        response1 = self.client.get(
            url, {"date": tomorrow.isoformat(), "participants": "5"}
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response1.data["results"]), 1)

        # Case 2: Search for tomorrow with 11 participants, should find nothing
        response2 = self.client.get(
            url, {"date": tomorrow.isoformat(), "participants": "11"}
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response2.data["results"]), 0)


# --- Tests for PublicScheduleViewSet ---


class PublicScheduleViewSetTest(APITestCase):
    """
    Tests for the public-facing schedule availability endpoint.
    """

    def setUp(self):
        self.tomorrow = timezone.now().date() + timedelta(days=1)
        self.day_after = self.tomorrow + timedelta(days=1)

        # Create a class and option to test against
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(category=self.category)
        self.option = ClassOptionFactory(classId=self.klass)

        # Create several instances for this option
        # Instance 1: Tomorrow morning, some spots taken
        inst1 = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=self.tomorrow,
            time=time(9, 0),
            max_participants=10,
        )
        BookingFactory(schedule_instance=inst1, participants=3)

        # Instance 2: Tomorrow afternoon, full
        inst2 = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=self.tomorrow,
            time=time(14, 0),
            max_participants=5,
        )
        BookingFactory(schedule_instance=inst2, participants=5)

        # Instance 3: Day after tomorrow, available
        ScheduleInstanceFactory(
            schedule__option=self.option,
            date=self.day_after,
            time=time(10, 0),
            max_participants=12,
        )

        # Instance for a different option, should not appear in results
        other_option = ClassOptionFactory(classId=self.klass)
        ScheduleInstanceFactory(
            schedule__option=other_option, date=self.tomorrow, time=time(11, 0)
        )

    def test_availability_action_requires_params(self):
        """
        GET /api/schedules/availability/ - Should fail without required query parameters.
        """
        url = reverse("public-schedule-availability")

        # Test without any params
        response_none = self.client.get(url)
        self.assertEqual(response_none.status_code, status.HTTP_400_BAD_REQUEST)

        # Test with only option_id
        response_option = self.client.get(url, {"option_id": self.option.pk})
        self.assertEqual(response_option.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "'option_id', 'start_date', and 'end_date' are required",
            response_option.data["error"],
        )

    def test_availability_for_valid_date_range(self):
        """
        GET /api/schedules/availability/?... - Should return correct available slots.
        """
        url = reverse("public-schedule-availability")
        query_params = {
            "option_id": self.option.pk,
            "start_date": self.tomorrow.isoformat(),
            "end_date": self.day_after.isoformat(),
        }

        response = self.client.get(url, query_params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The response should be a dict keyed by date
        data = response.data
        self.assertIn(self.tomorrow.isoformat(), data)
        self.assertIn(self.day_after.isoformat(), data)

        # Check tomorrow's slots
        tomorrow_slots = data[self.tomorrow.isoformat()]
        self.assertEqual(len(tomorrow_slots), 1)  # Only one has available spots

        available_slot = tomorrow_slots[0]
        self.assertEqual(available_slot["time"], "09:00:00")
        self.assertEqual(available_slot["available_spots"], 7)

        # Check day after's slots
        day_after_slots = data[self.day_after.isoformat()]
        self.assertEqual(len(day_after_slots), 1)
        self.assertEqual(day_after_slots[0]["available_spots"], 12)

    def test_availability_for_range_with_no_slots(self):
        """
        GET /api/schedules/availability/?... - Should return an empty object for a date range with no openings.
        """
        far_future_start = self.day_after + timedelta(days=30)
        far_future_end = far_future_start + timedelta(days=5)

        url = reverse("public-schedule-availability")
        query_params = {
            "option_id": self.option.pk,
            "start_date": far_future_start.isoformat(),
            "end_date": far_future_end.isoformat(),
        }

        response = self.client.get(url, query_params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {})
