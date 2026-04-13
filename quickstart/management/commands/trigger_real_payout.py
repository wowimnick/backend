# quickstart/management/commands/trigger_real_payout.py

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from datetime import timedelta
from decimal import Decimal
import stripe

from quickstart.models import Booking, Payout, Payment, BusinessInfo
from quickstart.tests.factories import BookingFactory, ScheduleInstanceFactory
from quickstart.tasks.payout_tasks import (
    update_completed_booking_status,
    process_daily_payouts,
)
from quickstart.utils.stripe_processing_fee import estimate_stripe_processing_fee

stripe.api_key = settings.STRIPE_SECRET_KEY


class Command(BaseCommand):
    help = (
        "Triggers a REAL payout flow against the Stripe Sandbox. "
        "Requires a valid test Stripe Connected Account ID."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "stripe_account_id",
            type=str,
            help="The Stripe Connected Account ID (acct_...) to send the payout to.",
        )

    def handle(self, *args, **options):
        stripe_account_id = options["stripe_account_id"]
        if not stripe_account_id.startswith("acct_"):
            raise CommandError(
                "Invalid Stripe Account ID provided. It must start with 'acct_'."
            )

        if "live" in settings.STRIPE_SECRET_KEY:
            raise CommandError(
                "SAFETY CHECK FAILED: You are using LIVE Stripe keys. This command is for sandbox testing only."
            )

        try:
            with transaction.atomic():
                self.stdout.write(
                    self.style.SUCCESS(
                        "--- Starting REAL Payout Flow Test Against Stripe Sandbox ---"
                    )
                )
                self._run_real_test(stripe_account_id)
                self.stdout.write(
                    self.style.WARNING(
                        "\nNOTE: This command created real test objects in your Stripe account."
                    )
                )
                raise CommandError(
                    "Test finished. Rolling back local database transaction to clean up test data."
                )
        except CommandError as e:
            self.stdout.write(self.style.SUCCESS(f"\n✅ {str(e)}"))
        except stripe.StripeError as e:
            self.stderr.write(
                self.style.ERROR(f"\n❌ A Stripe API error occurred: {e}")
            )
            self.stderr.write(
                self.style.WARNING(
                    "  - Did you provide a valid, fully onboarded test account ID?"
                )
            )
            self.stderr.write(
                self.style.WARNING("  - Check your Stripe Dashboard for more details.")
            )
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(f"\n❌ An unexpected error occurred: {e}")
            )

    def _run_real_test(self, stripe_account_id):
        # 1. SETUP
        self.stdout.write("STEP 1: Setting up local test data...")
        business = BusinessInfo.objects.filter(
            stripe_account_id=stripe_account_id
        ).first()
        if not business:
            raise CommandError(
                f"No business found in the database with Stripe ID: {stripe_account_id}"
            )

        self.stdout.write(f"  - Found Business: '{business.businessName}'")
        yesterday = timezone.now().date() - timedelta(days=1)
        gross_amount = Decimal("100.00")

        class_instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=business,
            date=yesterday,
            price=gross_amount,
        )
        self.stdout.write(f"  - Created Class Instance for date: {class_instance.date}")

        booking = BookingFactory(
            schedule_instance=class_instance,
            status="confirmed",
            payment_status="pending",
            payout_status="pending",
            amount_paid=gross_amount,
        )
        self.stdout.write(f"  - Created local Booking {booking.id}")

        # 2. SIMULATE CUSTOMER PAYMENT
        self.stdout.write(
            "\nSTEP 2: Simulating customer payment to fund platform balance..."
        )

        intent = stripe.PaymentIntent.create(
            amount=int(gross_amount * 100),
            currency=getattr(settings, "STRIPE_CURRENCY", "CAD").lower(),
            description="Test payment for payout flow",
            automatic_payment_methods={
                "enabled": True,
                "allow_redirects": "never",
            },
        )
        self.stdout.write(f"  - Created Payment Intent: {intent.id}")

        confirmed_intent = stripe.PaymentIntent.confirm(
            intent.id,
            payment_method="pm_card_visa",
        )
        self.stdout.write(
            f"  - Confirmed Payment Intent. Funds are now in platform's test balance."
        )

        proc_fee = estimate_stripe_processing_fee(gross_amount)
        net_to_business = (gross_amount - proc_fee).quantize(Decimal("0.01"))
        payment_record = Payment.objects.create(
            booking=booking,
            stripe_payment_intent_id=confirmed_intent.id,
            amount=gross_amount,
            status="succeeded",
            stripe_processing_fee=proc_fee,
            net_payout_amount=net_to_business,
        )
        booking.payment_status = "paid"
        booking.allocated_net_payout = net_to_business
        booking.save(update_fields=["payment_status", "allocated_net_payout"])
        self.stdout.write(
            f"  - Created local Payment record {payment_record.id} and updated booking status to 'paid'."
        )

        # 3. EXECUTE TASKS
        self.stdout.write("\nSTEP 3: Running Celery tasks...")
        update_completed_booking_status()
        self.stdout.write("  - Task 'update_completed_booking_status' finished.")

        summary = process_daily_payouts()
        self.stdout.write(
            f"  - Task 'process_daily_payouts' finished. Summary: '{summary}'"
        )
        if "Failed transfers: 1" in summary:
            raise CommandError("TEST FAILED: The payout task reported a failure.")

        # 4. VERIFY RESULTS
        self.stdout.write("\nSTEP 4: Verifying results with Stripe...")
        booking.refresh_from_db()
        assert booking.payout_status == "processed"
        self.stdout.write(
            self.style.SUCCESS(
                "  - PASSED: Local booking payout_status is 'processed'."
            )
        )

        payout = (
            Payout.objects.filter(business=business).order_by("-created_at").first()
        )
        if not payout:
            raise AssertionError(
                "Verification failed: Payout record was not created in the database."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"  - PASSED: Local Payout record {payout.id} was created."
            )
        )

        stripe_transfer_id = payout.stripe_transfer_id
        self.stdout.write(
            f"  - Retrieving Transfer '{stripe_transfer_id}' from Stripe API..."
        )

        # --- THIS IS THE FIX ---
        # Removed the unsupported 'use_stripe_sdk=True' parameter
        retrieved_transfer = stripe.Transfer.retrieve(stripe_transfer_id)
        # --- END OF FIX ---

        expected_payout = net_to_business
        retrieved_amount_decimal = Decimal(retrieved_transfer.amount) / 100

        self.stdout.write(
            f"    - Amount transferred (Stripe): ${retrieved_amount_decimal}"
        )
        self.stdout.write(f"    - Expected amount: ${expected_payout}")
        assert retrieved_amount_decimal == expected_payout

        self.stdout.write(
            f"    - Destination account (Stripe): {retrieved_transfer.destination}"
        )
        self.stdout.write(f"    - Expected destination: {stripe_account_id}")
        assert retrieved_transfer.destination == stripe_account_id

        self.stdout.write(
            self.style.SUCCESS(
                "  - PASSED: Retrieved Stripe Transfer matches expected amount and destination."
            )
        )
