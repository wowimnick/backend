import hashlib
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from quickstart.models import Payment, Payout
from quickstart.tasks.payout_tasks import (
    _estimate_stripe_processing_fee,
    process_daily_payouts,
    update_completed_booking_status,
)
from quickstart.tests.factories import (
    BookingFactory,
    BusinessFactory,
    ScheduleInstanceFactory,
)


class Command(BaseCommand):
    help = (
        "Sanity-check payout flow: completed booking → process_daily_payouts with mocked Stripe. "
        "Runs inside a transaction and rolls back (no persistent test data). "
        "Note: process_daily_payouts scans ALL businesses with pending payouts; the mock uses a "
        "unique transfer id per Connect destination so other dev data does not cause tr_ collisions."
    )

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                self.stdout.write(self.style.SUCCESS("--- Starting Payout Flow Test ---"))
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
            raise

    def _run_test(self):
        self.stdout.write("STEP 1: Setting up test data...")
        business = BusinessFactory(
            stripe_account_id="acct_test_payout_flow_123",
            stripe_account_status="active",
            currency="CAD",
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
            allocated_net_payout=Decimal("100.00"),
        )
        Payment.objects.create(
            booking=booking,
            stripe_payment_intent_id="pi_test_payout_flow_unique",
            amount=Decimal("100.00"),
            status="succeeded",
        )
        self.stdout.write(
            f"  - Created Booking {booking.id} with payout_status '{booking.payout_status}'"
        )

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

        fee = _estimate_stripe_processing_fee(Decimal("100.00"))
        expected_payout = (Decimal("100.00") - fee).quantize(Decimal("0.01"))
        expected_payout_cents = int(expected_payout * 100)

        def fake_stripe_transfer_create(*args, **kwargs):
            """
            Real task may pay multiple businesses in one run; Stripe transfer ids are unique.
            Return a stable-but-unique id per Connect destination so the DB unique constraint
            is never violated when other dev businesses also get a mocked transfer.
            """
            dest = str(kwargs.get("destination") or "")
            digest = hashlib.sha256(dest.encode()).hexdigest()[:24]
            transfer_id = f"tr_test_pf_{digest}"
            meta_in = kwargs.get("metadata") or {}

            class EchoMeta:
                def __init__(self, d):
                    self._d = dict(d) if hasattr(d, "items") else {}

                def to_dict(self):
                    return dict(self._d)

            m = MagicMock()
            m.id = transfer_id
            m.currency = "cad"
            m.metadata = EchoMeta(meta_in)
            return m

        self.stdout.write(
            "\nSTEP 3: Running task 'process_daily_payouts' with mocked Stripe + email..."
        )
        with patch("quickstart.utils.email_utils.send_templated_email"):
            with patch(
                "stripe.Transfer.create", side_effect=fake_stripe_transfer_create
            ) as mock_stripe:
                summary = process_daily_payouts()

        self.stdout.write(f"  - Task execution summary: '{summary}'")
        if "Failed: 0" not in summary:
            raise CommandError(
                self.style.ERROR(
                    "TEST FAILED: process_daily_payouts reported failures. Check logs."
                )
            )

        self.stdout.write("\nSTEP 4: Verifying results...")
        our_calls = [
            c
            for c in mock_stripe.call_args_list
            if c.kwargs.get("destination") == business.stripe_account_id
        ]
        if len(our_calls) != 1:
            raise AssertionError(
                f"Expected exactly one Transfer.create for test business destination; got {len(our_calls)}"
            )
        call_args = our_calls[0].kwargs
        self.stdout.write(f"    - Est. Stripe processing fee on charge: ${fee}")
        self.stdout.write(f"    - Expected net transfer: ${expected_payout}")
        assert call_args["amount"] == expected_payout_cents
        assert call_args["destination"] == business.stripe_account_id
        self.stdout.write(
            self.style.SUCCESS(
                "    - PASSED: stripe.Transfer.create amount and destination for this business."
            )
        )

        booking.refresh_from_db()
        assert booking.payout_status == "processed"
        self.stdout.write(
            self.style.SUCCESS(
                f"    - PASSED: Booking {booking.id} payout_status is 'processed'."
            )
        )

        expected_tr = fake_stripe_transfer_create(
            destination=business.stripe_account_id
        ).id
        payout = Payout.objects.filter(
            business=business, stripe_transfer_id=expected_tr
        ).first()
        if not payout:
            raise AssertionError("Payout row missing after successful transfer.")
        # Task passes business.businessId (int) into Transfer metadata; JSONField keeps int.
        assert str(payout.metadata.get("business_id")) == str(business.businessId)
        self.stdout.write(
            self.style.SUCCESS(f"    - PASSED: Payout {payout.id} with merged metadata.")
        )

        assert payout.bookings.filter(id=booking.id).exists()
        self.stdout.write(
            self.style.SUCCESS("    - PASSED: Booking linked to Payout.")
        )
