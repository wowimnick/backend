from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from quickstart.models import Booking, Payout
from quickstart.tests.factories import (
    BusinessInfoFactory,
    BookingFactory,
    ScheduleInstanceFactory,
)
from quickstart.tasks.payout_tasks import (
    update_completed_booking_status,
    process_daily_payouts,
    PLATFORM_FEE_RATE,
)


class Command(BaseCommand):
    help = "Tests the end-to-end payout flow for a class completed yesterday."

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                self.stdout.write(
                    self.style.SUCCESS("--- Starting Payout Flow Test ---")
                )
                self._run_test()
                raise CommandError(
                    "Test finished. Rolling back transaction to clean up test data."
                )
        except CommandError as e:
            self.stdout.write(self.style.SUCCESS(f"\n✅ {str(e)}"))
        except Exception as e:
            self.stderr.write(
                self.style.ERROR(
                    f"An unexpected error occurred during test verification: {e}"
                )
            )

    def _run_test(self):
        # 1. SETUP
        self.stdout.write("STEP 1: Setting up test data...")
        business = BusinessInfoFactory(
            stripe_account_id="acct_test_123456789", stripe_account_status="active"
        )
        self.stdout.write(
            f"  - Created Business: '{business.businessName}' (Stripe ID: {business.stripe_account_id})"
        )
        yesterday = timezone.now().date() - timedelta(days=1)
        class_instance = ScheduleInstanceFactory(
            schedule__option__classId__businessId=business,
            date=yesterday,
            price=Decimal("100.00"),
        )
        self.stdout.write(f"  - Created Class Instance for date: {class_instance.date}")
        booking = BookingFactory(
            schedule_instance=class_instance,
            status="confirmed",
            payment_status="paid",
            payout_status="pending",
            amount_paid=Decimal("100.00"),
        )
        self.stdout.write(
            f"  - Created Booking {booking.id} with status '{booking.status}' and payout_status '{booking.payout_status}'"
        )

        # 2. EXECUTE TASK 1
        self.stdout.write("\nSTEP 2: Running task 'update_completed_booking_status'...")
        update_completed_booking_status()
        booking.refresh_from_db()
        self.stdout.write(f"  - Booking {booking.id} status is now: '{booking.status}'")
        assert booking.status == "completed"
        self.stdout.write(
            self.style.SUCCESS(
                "  - Task 1 PASSED: Booking status correctly updated to 'completed'."
            )
        )

        # 3. EXECUTE TASK 2
        self.stdout.write(
            "\nSTEP 3: Running task 'process_daily_payouts' with mocked Stripe API..."
        )
        with patch("stripe.Transfer.create") as mock_stripe_transfer:
            mock_transfer_object = MagicMock(
                id="tr_test_payout_123",
                currency="cad",
                arrival_date=int((timezone.now() + timedelta(days=2)).timestamp()),
                status="pending",
            )
            mock_stripe_transfer.return_value = mock_transfer_object

            # Run the task and store the summary message it returns
            summary = process_daily_payouts()

            self.stdout.write(f"  - Task execution summary: '{summary}'")
            # --- IMPROVED VERIFICATION ---
            if "Failed transfers: 0" not in summary:
                # If the task reported a failure, we stop the test here with a clear error.
                raise CommandError(
                    self.style.ERROR(
                        "TEST FAILED: The payout task reported a failure. Check logs for details."
                    )
                )

            # 4. VERIFY RESULTS
            self.stdout.write("\nSTEP 4: Verifying results...")
            self.stdout.write("  - Verifying Stripe API call...")
            mock_stripe_transfer.assert_called_once()
            call_args = mock_stripe_transfer.call_args[1]
            expected_payout = (
                booking.amount_paid * (Decimal("1.0") - PLATFORM_FEE_RATE)
            ).quantize(Decimal("0.01"))
            expected_payout_cents = int(expected_payout * 100)
            self.stdout.write(f"    - Gross revenue: ${booking.amount_paid}")
            self.stdout.write(
                f"    - Platform fee ({PLATFORM_FEE_RATE*100}%): ${booking.amount_paid * PLATFORM_FEE_RATE}"
            )
            self.stdout.write(f"    - Expected Net Payout: ${expected_payout}")
            assert call_args["amount"] == expected_payout_cents
            assert call_args["destination"] == business.stripe_account_id
            self.stdout.write(
                self.style.SUCCESS(
                    "    - PASSED: stripe.Transfer.create called with correct amount and destination."
                )
            )

            self.stdout.write("  - Verifying database state...")
            booking.refresh_from_db()
            assert booking.payout_status == "processed"
            self.stdout.write(
                self.style.SUCCESS(
                    f"    - PASSED: Booking {booking.id} payout_status is now '{booking.payout_status}'."
                )
            )

            payout = Payout.objects.filter(
                business=business, stripe_transfer_id="tr_test_payout_123"
            ).first()
            if not payout:
                raise AssertionError(
                    "Verification failed: Payout record was not created in the database."
                )
            self.stdout.write(
                self.style.SUCCESS(
                    f"    - PASSED: Payout record {payout.id} created successfully."
                )
            )

            booking_in_payout = payout.bookings.filter(id=booking.id).exists()
            assert booking_in_payout
            self.stdout.write(
                self.style.SUCCESS(
                    "    - PASSED: Booking is correctly linked to the new Payout record."
                )
            )
