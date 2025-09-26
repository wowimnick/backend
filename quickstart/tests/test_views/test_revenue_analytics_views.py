# quickstart/tests/test_views/test_revenue_analytics_views.py

from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import csv
from io import StringIO
from django.contrib.auth.models import Permission

from quickstart.models import BusinessInfo, Role, PartnerTier, Booking, Payment
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    RoleFactory,
    BookingFactory,
    PaymentFactory,
    PartnerTierFactory,
)


class RevenueAnalyticsViewTests(APITestCase):
    """
    [NEW] Tests for the business-facing revenue analytics endpoint.
    """

    def setUp(self):
        self.user = UserFactory()
        business_role = RoleFactory(name="Business Owner")
        # Add relevant permissions
        self.user.role = business_role
        self.user.save()
        self.tier = PartnerTierFactory(fee_percentage=Decimal("10.00"))
        self.business = BusinessInfoFactory(owner=self.user, partner_tier=self.tier)
        self.client.force_authenticate(user=self.user)
        self.url = reverse("revenue-analytics")

        # Create some data in the last 30 days
        booking1 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            booking_date=timezone.now() - timedelta(days=5),
            amount_paid=Decimal("100.00"),
            payment_status="paid",
        )
        PaymentFactory(booking=booking1, amount=Decimal("113.00"))

        booking2 = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            booking_date=timezone.now() - timedelta(days=15),
            amount_paid=Decimal("200.00"),
            payment_status="paid",
        )
        PaymentFactory(booking=booking2, amount=Decimal("226.00"))

        # Create data for the previous period to test growth calculation
        booking_prev = BookingFactory(
            schedule_instance__schedule__option__classId__businessId=self.business,
            booking_date=timezone.now() - timedelta(days=40),
            amount_paid=Decimal("150.00"),
            payment_status="paid",
        )
        PaymentFactory(booking=booking_prev, amount=Decimal("169.50"))
