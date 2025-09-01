import zoneinfo
from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock
from decimal import Decimal
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
import pytz

from quickstart.models import (
    Booking,
    BusinessInfo,
    CustomUser,
    Payment,
    Role,
    PartnerTier,
)
from quickstart.tests.factories import (
    UserFactory,
    BusinessInfoFactory,
    ClassesMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
    RoleFactory,
    BookingFactory,
    ClassCategoryFactory,
    PartnerTierFactory,
)


def _get_and_assign_permission(role, perm_codename, model_class):
    """Helper to get or create a permission and assign it to a role."""
    content_type = ContentType.objects.get_for_model(model_class)
    permission, _ = Permission.objects.get_or_create(
        codename=perm_codename,
        content_type=content_type,
    )
    role.permissions.add(permission)


class BookingFlowTests(APITestCase):
    """
    Tests the user-facing booking creation and management flow.
    """

    def setUp(self):
        # Create roles
        student_role = RoleFactory(name="Student", is_default=True)
        _get_and_assign_permission(student_role, "add_booking", Booking)
        _get_and_assign_permission(student_role, "cancel_own_booking", CustomUser)

        self.business_owner_role = RoleFactory(
            name="Business Owner", hierarchy_level=50
        )
        _get_and_assign_permission(
            self.business_owner_role, "view_own_business_bookings", BusinessInfo
        )
        _get_and_assign_permission(
            self.business_owner_role, "cancel_business_booking", Booking
        )

        # Create users and assign roles
        self.student = UserFactory(role=student_role)
        self.owner = UserFactory(role=self.business_owner_role)

        self.student.user_permissions.add(*student_role.permissions.all())
        self.owner.user_permissions.add(*self.business_owner_role.permissions.all())

        # Create business and class structure
        self.business = BusinessInfoFactory(owner=self.owner)
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(
            businessId=self.business, category=self.category
        )
        self.option = ClassOptionFactory(classId=self.klass, cancellationPolicy="24h")

        # Create schedule instances for testing
        self.future_instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() + timedelta(days=10),
            max_participants=5,
        )
        self.past_instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() - timedelta(days=1),
        )
        self.full_instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() + timedelta(days=5),
            max_participants=2,
        )
        BookingFactory(
            schedule_instance=self.full_instance,
            participants=2,
            status="confirmed",
        )

    def test_student_can_create_pending_booking(self):
        print("\n--- Running: test_student_can_create_pending_booking ---")
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-list")
        data = {
            "selectedSlots": [{"id": self.future_instance.id}],
            "participants": 2,
            "participant_details": [{"name": "John Doe"}, {"name": "Jane Doe"}],
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            Booking.objects.filter(
                user=self.student,
                schedule_instance=self.future_instance,
                status="pending",
            ).exists()
        )
        print("✅ PASSED: Student can create a pending booking.")

    def test_booking_fails_for_full_class(self):
        print("\n--- Running: test_booking_fails_for_full_class ---")
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-list")
        data = {
            "selectedSlots": [{"id": self.full_instance.id}],
            "participants": 1,
            "participant_details": [{"name": "Late Comer"}],
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Not enough spots", str(response.data))
        print("✅ PASSED: Booking correctly fails for a full class.")

    def test_cancellation_policy_respects_business_timezone(
        self,
    ):
        print("\n--- Running: test_cancellation_policy_respects_business_timezone ---")
        ny_tz = zoneinfo.ZoneInfo("America/New_York")
        self.business.business_timezone = "America/New_York"
        self.business.save()
        self.option.cancellationPolicy = "24h"
        self.option.save()

        class_datetime_ny = timezone.datetime(2025, 7, 4, 14, 0, 0, tzinfo=ny_tz)
        instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=class_datetime_ny.date(),
            time=class_datetime_ny.time(),
        )
        booking = BookingFactory(
            user=self.student,
            schedule_instance=instance,
            status="confirmed",
            cancellation_policy="24h",
        )
        url = reverse("my-booking-student-cancel", kwargs={"pk": booking.pk})
        self.client.force_authenticate(user=self.student)

        cancellation_fail_time = class_datetime_ny - timedelta(hours=23)
        with patch("django.utils.timezone.now") as mock_now_fail:
            mock_now_fail.return_value = cancellation_fail_time
            response_fail = self.client.post(url, {}, format="json")

        self.assertEqual(response_fail.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("policy", str(response_fail.data))

        cancellation_success_time = class_datetime_ny - timedelta(hours=25)
        with patch("django.utils.timezone.now") as mock_now_success:
            mock_now_success.return_value = cancellation_success_time
            response_success = self.client.post(url, {}, format="json")

        self.assertEqual(response_success.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

        print("✅ PASSED: Cancellation policy correctly enforced across timezones.")

    def test_booking_fails_for_past_class(self):
        print("\n--- Running: test_booking_fails_for_past_class ---")
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-list")
        data = {
            "selectedSlots": [{"id": self.past_instance.id}],
            "participants": 1,
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot book past session", str(response.data))
        print("✅ PASSED: Booking correctly fails for a past class.")

    def test_student_can_view_own_bookings(self):
        print("\n--- Running: test_student_can_view_own_bookings ---")
        BookingFactory(user=self.student, schedule_instance=self.future_instance)
        BookingFactory()

        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 1)
        print("✅ PASSED: Student can view their own list of bookings.")

    def test_student_can_cancel_booking_within_policy(self):
        print("\n--- Running: test_student_can_cancel_booking_within_policy ---")
        booking = BookingFactory(
            user=self.student,
            schedule_instance=self.future_instance,
            status="confirmed",
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-student-cancel", kwargs={"pk": booking.pk})

        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        print("✅ PASSED: Student can cancel a booking within the policy timeframe.")

    def test_student_cannot_cancel_booking_outside_policy(self):
        print("\n--- Running: test_student_cannot_cancel_booking_outside_policy ---")
        instance_too_soon = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() + timedelta(days=1),
            time=(timezone.now() - timedelta(hours=1)).time(),
        )
        booking = BookingFactory(
            user=self.student,
            schedule_instance=instance_too_soon,
            status="confirmed",
            cancellation_policy="24h",
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-student-cancel", kwargs={"pk": booking.pk})

        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("policy", str(response.data))
        print("✅ PASSED: Student correctly prevented from cancelling outside policy.")

    def test_student_cannot_cancel_booking_just_past_boundary(self):
        print(
            "\n--- Running: test_student_cannot_cancel_booking_just_past_boundary ---"
        )
        utc_tz = zoneinfo.ZoneInfo("UTC")
        class_time = timezone.datetime(2025, 1, 15, 15, 0, 0, tzinfo=utc_tz)

        instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=class_time.date(),
            time=class_time.time(),
        )
        booking = BookingFactory(
            user=self.student,
            schedule_instance=instance,
            status="confirmed",
            cancellation_policy="24h",
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-student-cancel", kwargs={"pk": booking.pk})

        cancellation_time = class_time - timedelta(hours=23, minutes=59)

        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value = cancellation_time
            response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("policy", str(response.data).lower())
        print(
            "✅ PASSED: Student correctly blocked from cancelling just inside the policy window."
        )

    def test_student_can_cancel_booking_at_boundary(self):
        print("\n--- Running: test_student_can_cancel_booking_at_boundary ---")
        utc_tz = zoneinfo.ZoneInfo("UTC")
        class_time = timezone.datetime(2025, 1, 15, 15, 0, 0, tzinfo=utc_tz)
        instance = ScheduleInstanceFactory(
            schedule__option=self.option, date=class_time.date(), time=class_time.time()
        )
        booking = BookingFactory(
            user=self.student, schedule_instance=instance, status="confirmed"
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("my-booking-student-cancel", kwargs={"pk": booking.pk})

        cancellation_time = class_time - timedelta(hours=24, minutes=1)
        with patch("django.utils.timezone.now") as mock_now:
            mock_now.return_value = cancellation_time
            response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        print(
            "✅ PASSED: Student can successfully cancel just outside the policy window."
        )

    def test_business_owner_can_view_business_bookings(self):
        print("\n--- Running: test_business_owner_can_view_business_bookings ---")
        isolated_owner = UserFactory(role=self.business_owner_role)
        isolated_owner.user_permissions.add(*self.business_owner_role.permissions.all())
        isolated_business = BusinessInfoFactory(owner=isolated_owner)
        isolated_option = ClassOptionFactory(classId__businessId=isolated_business)

        BookingFactory.create_batch(
            2, schedule_instance__schedule__option=isolated_option
        )
        BookingFactory()

        self.client.force_authenticate(user=isolated_owner)
        url = reverse("business-booking-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)
        print("✅ PASSED: Business owner can view all bookings for their business.")

    def test_business_owner_can_cancel_booking(self):
        print("\n--- Running: test_business_owner_can_cancel_booking ---")
        booking = BookingFactory(
            schedule_instance__schedule__option=self.option,
            status="confirmed",
            payment_status="paid",
        )
        self.client.force_authenticate(user=self.owner)
        url = reverse("business-booking-business-cancel", kwargs={"pk": booking.pk})
        data = {"reason": "Instructor is unavailable."}

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.payment_status, "refund_pending")
        self.assertIn(data["reason"], booking.cancellation_reason)
        print("✅ PASSED: Business owner can cancel a booking.")


class PaymentFlowTests(APITestCase):
    """
    Tests the payment intent creation and webhook processing.
    """

    def setUp(self):
        student_role = RoleFactory(name="Student", is_default=True)
        # Permissions are not directly tested here but good practice to keep
        self.student = UserFactory(role=student_role)

        # Create Partner Tiers for testing different fee scenarios
        self.standard_tier = PartnerTierFactory(
            name="Standard", fee_percentage=Decimal("13.00"), is_default=True
        )
        self.founding_tier = PartnerTierFactory(
            name="Founding Partner", fee_percentage=Decimal("0.00")
        )
        self.premium_tier = PartnerTierFactory(
            name="Premium Partner", fee_percentage=Decimal("10.00")
        )

        # Create a business with the standard tier by default
        self.business = BusinessInfoFactory(partner_tier=self.standard_tier)
        self.category = ClassCategoryFactory()
        self.klass = ClassesMainFactory(
            businessId=self.business, category=self.category
        )
        self.option = ClassOptionFactory(classId=self.klass)
        self.instance = ScheduleInstanceFactory(
            schedule__option=self.option,
            date=timezone.now().date() + timedelta(days=10),
            price=Decimal("50.00"),
        )

    @patch("stripe.PaymentIntent.create")
    def test_create_payment_intent_successfully(self, mock_stripe_create):
        print("\n--- Running: test_create_payment_intent_successfully ---")
        mock_stripe_create.return_value = MagicMock(
            client_secret="test_client_secret_123", id="pi_12345"
        )
        self.client.force_authenticate(user=self.student)
        url = reverse("create-payment-intent")
        data = {
            "selectedSlots": [{"id": self.instance.id}],
            "participants": 2,
            "participant_details": [{"name": "John Doe"}, {"name": "Jane Doe"}],
        }

        response = self.client.post(url, data, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["clientSecret"], "test_client_secret_123")

        # --- FIX: Verify grand total including HST ---
        # Subtotal = 50.00 * 2 = 100.00
        # Tax (13%) = 13.00
        # Grand Total = 113.00
        self.assertEqual(response.data["amount"], 113.00)

        mock_stripe_create.assert_called_once()
        call_args = mock_stripe_create.call_args[1]
        self.assertEqual(call_args["amount"], 11300)  # Amount in cents
        self.assertEqual(call_args["metadata"]["user_id"], str(self.student.userId))
        self.assertEqual(call_args["metadata"]["subtotal_after_discount"], "100.00")
        self.assertEqual(call_args["metadata"]["tax_amount"], "13.00")
        print("✅ PASSED: Payment intent created successfully with HST.")

    @patch("stripe.Charge.retrieve")
    @patch("stripe.Webhook.construct_event")
    def test_webhook_payment_succeeded_confirms_booking(
        self, mock_construct_event, mock_charge_retrieve
    ):
        print("\n--- Running: test_webhook_payment_succeeded_confirms_booking ---")
        payment_intent_id = "pi_test_success_123"
        subtotal = self.instance.price
        tax = (subtotal * Decimal("0.13")).quantize(Decimal("0.01"))
        grand_total = subtotal + tax

        metadata = {
            "user_id": str(self.student.userId),
            "first_slot_id": str(self.instance.id),
            "participants": "1",
            "booking_type": "Single Session",
            "is_course": "False",
            "schedule_id": str(self.instance.schedule.id),
            "start_date": self.instance.date.isoformat(),
            "subtotal_after_discount": str(subtotal),
            "tax_amount": str(tax),
            "hst_rate": "0.13",
        }
        mock_event = MagicMock()
        mock_event.type = "payment_intent.succeeded"
        mock_payment_intent_object = MagicMock(
            id=payment_intent_id,
            amount_received=int(grand_total * 100),
            currency="cad",
            metadata=metadata,
            latest_charge="ch_123",
            payment_method_types=["card"],
        )
        mock_event.data.object = mock_payment_intent_object
        mock_construct_event.return_value = mock_event

        # FIX: Provide a more detailed mock for the Charge object to avoid errors
        mock_charge_retrieve.return_value = MagicMock(
            receipt_url="https://stripe.com/receipt/test",
            receipt_number="123-456-789",
            billing_details={"name": "Test User", "email": "test@example.com"},
            payment_method_details=MagicMock(
                type="card",
                card=MagicMock(
                    brand="visa",
                    last4="4242",
                    exp_month=12,
                    exp_year=2028,
                ),
            ),
        )

        url = reverse("payment-webhook")
        response = self.client.post(url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        booking = Booking.objects.get(
            user=self.student, schedule_instance=self.instance
        )
        self.assertEqual(booking.status, "confirmed")

        payment = Payment.objects.get(stripe_payment_intent_id=payment_intent_id)
        self.assertEqual(payment.status, "succeeded")
        # FIX: Assert new financial fields
        self.assertAlmostEqual(payment.amount, grand_total, places=2)
        self.assertAlmostEqual(payment.tax_amount, tax, places=2)
        # Fee: 13% of 50.00 = 6.50
        self.assertAlmostEqual(payment.platform_fee_amount, Decimal("6.50"), places=2)
        # Fee Tax: 13% of 6.50 = 0.845 -> 0.84
        self.assertAlmostEqual(payment.platform_fee_tax, Decimal("0.84"), places=2)
        # Payout: (50.00 - 6.50) + (6.50 - 0.84) = 43.50 + 5.66 = 49.16
        self.assertAlmostEqual(payment.net_payout_amount, Decimal("49.16"), places=2)

        print("✅ PASSED: Webhook correctly confirms booking and is idempotent.")

    @patch("quickstart.payments.views.ProcessBookingWebhook._attempt_stripe_refund")
    @patch("stripe.Webhook.construct_event")
    def test_webhook_refunds_on_booking_failure(
        self, mock_construct_event, mock_refund
    ):
        print("\n--- Running: test_webhook_refunds_on_booking_failure ---")
        self.instance.max_participants = 1
        self.instance.save()
        BookingFactory(
            schedule_instance=self.instance, participants=1, status="confirmed"
        )
        payment_intent_id = "pi_test_fail_456"

        # FIX: Add missing metadata required by the view logic
        metadata = {
            "user_id": str(self.student.userId),
            "first_slot_id": str(self.instance.id),
            "participants": "1",
            "booking_type": "Single Session",
            "is_course": "False",
            "schedule_id": str(self.instance.schedule.id),
            "start_date": self.instance.date.isoformat(),
            "subtotal_after_discount": "50.00",
            "tax_amount": "6.50",
            "hst_rate": "0.13",
        }
        mock_event = MagicMock()
        mock_event.type = "payment_intent.succeeded"
        mock_payment_intent_object = MagicMock()
        mock_payment_intent_object.id = payment_intent_id
        mock_payment_intent_object.metadata = metadata
        mock_event.data.object = mock_payment_intent_object
        mock_construct_event.return_value = mock_event

        url = reverse("payment-webhook")
        response = self.client.post(url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is now full", str(response.data))
        mock_refund.assert_called_once()
        args, kwargs = mock_refund.call_args
        self.assertEqual(args[0], payment_intent_id)
        self.assertIn("Booking validation failed", args[1])

        print("✅ PASSED: Webhook triggers refund on booking validation failure.")

    @patch("stripe.Charge.retrieve")
    @patch("stripe.Webhook.construct_event")
    def test_webhook_applies_zero_fee_for_founding_partner_tier(
        self, mock_construct_event, mock_charge_retrieve
    ):
        print(
            "\n--- Running: test_webhook_applies_zero_fee_for_founding_partner_tier ---"
        )
        founding_business = BusinessInfoFactory(partner_tier=self.founding_tier)
        founding_instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=founding_business,
            price=Decimal("100.00"),
        )
        payment_intent_id = "pi_test_founding_789"
        subtotal = founding_instance.price
        tax = (subtotal * Decimal("0.13")).quantize(Decimal("0.01"))
        grand_total = subtotal + tax

        metadata = {
            "user_id": str(self.student.userId),
            "first_slot_id": str(founding_instance.id),
            "participants": "1",
            "booking_type": "Single Session",
            "is_course": "False",
            "schedule_id": str(founding_instance.schedule.id),
            "start_date": founding_instance.date.isoformat(),
            "subtotal_after_discount": str(subtotal),
            "tax_amount": str(tax),
            "hst_rate": "0.13",
        }
        mock_event = MagicMock()
        mock_event.type = "payment_intent.succeeded"
        # FIX: Add payment_method_types to the mock to prevent insertion errors
        mock_event.data.object = MagicMock(
            id=payment_intent_id,
            amount_received=int(grand_total * 100),
            currency="cad",
            metadata=metadata,
            latest_charge="ch_founding_789",
            payment_method_types=["card"],
        )
        mock_construct_event.return_value = mock_event
        # FIX: Provide a complete mock for the Charge object.
        mock_charge_retrieve.return_value = MagicMock(
            receipt_url="https://stripe.com/receipt/founding",
            receipt_number="789-founding",
            billing_details={"name": "Founding User", "email": "founding@example.com"},
            payment_method_details=MagicMock(
                type="card",
                card=MagicMock(brand="amex", last4="0005", exp_month=1, exp_year=2030),
            ),
        )

        url = reverse("payment-webhook")
        response = self.client.post(url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payment = Payment.objects.get(stripe_payment_intent_id=payment_intent_id)
        self.assertEqual(payment.status, "succeeded")
        # FIX: Assert new financial fields for 0% fee
        self.assertAlmostEqual(payment.amount, grand_total, places=2)
        self.assertAlmostEqual(payment.tax_amount, tax, places=2)
        self.assertEqual(payment.platform_fee_amount, Decimal("0.00"))
        self.assertEqual(payment.platform_fee_tax, Decimal("0.00"))
        self.assertAlmostEqual(payment.net_payout_amount, grand_total, places=2)

        print("✅ PASSED: Webhook correctly applies 0% fee for Founding Partner tier.")

    @patch("stripe.Charge.retrieve")
    @patch("stripe.Webhook.construct_event")
    def test_webhook_applies_correct_fee_for_premium_tier(
        self, mock_construct_event, mock_charge_retrieve
    ):
        print("\n--- Running: test_webhook_applies_correct_fee_for_premium_tier ---")
        premium_business = BusinessInfoFactory(partner_tier=self.premium_tier)
        premium_instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=premium_business,
            price=Decimal("200.00"),
        )
        payment_intent_id = "pi_test_premium_101"
        subtotal = premium_instance.price
        tax = (subtotal * Decimal("0.13")).quantize(Decimal("0.01"))
        grand_total = subtotal + tax

        metadata = {
            "user_id": str(self.student.userId),
            "first_slot_id": str(premium_instance.id),
            "participants": "1",
            "booking_type": "Single Session",
            "is_course": "False",
            "schedule_id": str(premium_instance.schedule.id),
            "start_date": premium_instance.date.isoformat(),
            "subtotal_after_discount": str(subtotal),
            "tax_amount": str(tax),
            "hst_rate": "0.13",
        }
        mock_event = MagicMock()
        mock_event.type = "payment_intent.succeeded"
        # FIX: Add payment_method_types to the mock to prevent insertion errors
        mock_event.data.object = MagicMock(
            id=payment_intent_id,
            amount_received=int(grand_total * 100),
            currency="cad",
            metadata=metadata,
            latest_charge="ch_premium_101",
            payment_method_types=["card"],
        )
        mock_construct_event.return_value = mock_event
        # FIX: Provide a complete mock for the Charge object.
        mock_charge_retrieve.return_value = MagicMock(
            receipt_url="https://stripe.com/receipt/premium",
            receipt_number="101-premium",
            billing_details={"name": "Premium User", "email": "premium@example.com"},
            payment_method_details=MagicMock(
                type="card",
                card=MagicMock(
                    brand="mastercard", last4="5555", exp_month=6, exp_year=2029
                ),
            ),
        )

        url = reverse("payment-webhook")
        response = self.client.post(url, data={}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        payment = Payment.objects.get(stripe_payment_intent_id=payment_intent_id)
        self.assertEqual(payment.status, "succeeded")
        # FIX: Assert new financial fields for 10% fee
        self.assertAlmostEqual(payment.amount, grand_total, places=2)  # 226.00
        self.assertAlmostEqual(payment.tax_amount, tax, places=2)  # 26.00
        # Fee: 10% of 200.00 = 20.00
        self.assertEqual(payment.platform_fee_amount, Decimal("20.00"))
        # Fee Tax: 13% of 20.00 = 2.60
        self.assertEqual(payment.platform_fee_tax, Decimal("2.60"))
        # Payout: (200.00 - 20.00) + (26.00 - 2.60) = 180 + 23.40 = 203.40
        self.assertAlmostEqual(payment.net_payout_amount, Decimal("203.40"), places=2)

        print("✅ PASSED: Webhook correctly applies 10% fee for Premium Partner tier.")
