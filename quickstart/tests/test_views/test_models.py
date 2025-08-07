# quickstart/tests/test_models.py

from django.test import TestCase
from django.utils import timezone
from decimal import Decimal

from quickstart.models import (
    ScheduleInstance,
    Booking,
    BusinessInfo,
    Reviews,
    BlogPost,
)
from quickstart.tests.factories import (
    ScheduleInstanceFactory,
    BookingFactory,
    BusinessInfoFactory,
    ReviewFactory,
    BlogPostFactory,
)


class ModelMethodTests(TestCase):
    def test_schedule_instance_delete_cancels_bookings_and_marks_for_refund(self):
        """
        Unit test the delete() method of the ScheduleInstance model.
        """
        # 1. Setup
        instance = ScheduleInstanceFactory(price=Decimal("50.00"))
        # Booking that was paid and should be marked for refund
        booking_paid = BookingFactory(
            schedule_instance=instance,
            status="confirmed",
            payment_status="paid",
            amount_paid=Decimal("50.00"),
        )
        # Booking that was pending and should just be cancelled
        booking_pending = BookingFactory(
            schedule_instance=instance, status="pending", payment_status="pending"
        )
        # A booking that is already cancelled and should be ignored
        BookingFactory(schedule_instance=instance, status="cancelled")

        # 2. Action
        instance.delete()

        # 3. Assertions
        # Refresh from DB to get the updated state
        booking_paid.refresh_from_db()
        booking_pending.refresh_from_db()

        # Check paid booking
        self.assertEqual(booking_paid.status, "cancelled")
        self.assertEqual(booking_paid.payment_status, "refund_pending")
        self.assertIsNotNone(booking_paid.cancelled_at)
        self.assertIn("Session instance was removed", booking_paid.cancellation_reason)

        # Check pending booking
        self.assertEqual(booking_pending.status, "cancelled")
        self.assertEqual(
            booking_pending.payment_status, "pending"
        )  # Payment status shouldn't change

        # Verify the instance itself is gone
        self.assertFalse(ScheduleInstance.objects.filter(pk=instance.pk).exists())

    def test_business_review_aggregates_update_correctly(self):
        """
        Unit test the update_review_aggregates() method of the BusinessInfo model.
        """
        # 1. Setup
        business = BusinessInfoFactory()
        # Initial state should be 0
        self.assertEqual(business.total_reviews_count, 0)
        self.assertEqual(business.average_rating, Decimal("0.0"))

        # 2. Action: Create reviews
        ReviewFactory(classId__businessId=business, status="approved", rating=5)
        ReviewFactory(classId__businessId=business, status="approved", rating=4)
        ReviewFactory(classId__businessId=business, status="approved", rating=4)
        ReviewFactory(
            classId__businessId=business, status="under_review", rating=1
        )  # Should be ignored

        # 3. Trigger update
        business.update_review_aggregates()

        # 4. Assertions
        business.refresh_from_db()
        self.assertEqual(business.total_reviews_count, 3)
        # Average of (5+4+4)/3 = 4.333..., rounded to 1 decimal place
        self.assertEqual(business.average_rating, Decimal("4.3"))

    def test_blog_post_unique_slug_generation(self):
        """
        Unit test the slug generation logic in the BlogPost model's save() method.
        """
        # Create the first post
        post1 = BlogPostFactory(title="A Cool Title")
        self.assertEqual(post1.slug, "a-cool-title")

        # Create a second post with the same title
        post2 = BlogPostFactory(title="A Cool Title")

        # Assert that the slug is different
        self.assertNotEqual(post2.slug, "a-cool-title")
        self.assertTrue(post2.slug.startswith("a-cool-title-"))
