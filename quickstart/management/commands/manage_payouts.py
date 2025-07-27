import random
import json  # Import the json library for pretty-printing
from datetime import timedelta
from decimal import Decimal

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, Payout, Payment


class Command(BaseCommand):
    help = "Creates and deletes test payout data for a specific business. Can optionally trigger real Stripe sandbox transfers."

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            type=str,
            choices=["create", "delete"],
            help="The action to perform: 'create' or 'delete' payouts.",
        )
        parser.add_argument(
            "business_id",
            type=int,
            help="The ID of the BusinessInfo object to target.",
        )
        parser.add_argument(
            "--count",
            type=int,
            default=1,
            help="The number of payouts to create (default: 1).",
        )
        parser.add_argument(
            "--status",
            type=str,
            choices=["paid", "pending", "in_transit", "failed"],
            help="[FAKE DATA ONLY] Force a specific status. This will not make a real Stripe call.",
        )
        parser.add_argument(
            "--real-stripe",
            action="store_true",
            help="Trigger a REAL Stripe transfer in your SANDBOX environment. Requires DEBUG=True.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        action = options["action"]
        business_id = options["business_id"]

        try:
            business = BusinessInfo.objects.get(pk=business_id)
        except BusinessInfo.DoesNotExist:
            raise CommandError(f'Business with ID "{business_id}" does not exist.')

        if action == "create":
            self.create_test_payouts(business, options)
        elif action == "delete":
            self.delete_test_payouts(business)

    def create_test_payouts(self, business, options):
        count = options["count"]
        use_real_stripe = options["real_stripe"]
        forced_status = options.get("status")

        if use_real_stripe:
            if not settings.DEBUG:
                raise CommandError("Cannot use --real-stripe when DEBUG is False.")
            if not business.stripe_account_id:
                raise CommandError(
                    f"Business '{business.businessName}' has no Stripe Account ID."
                )
            if forced_status:
                raise CommandError("Cannot use --status and --real-stripe together.")
            self.stdout.write(
                self.style.WARNING("--- REAL STRIPE SANDBOX MODE ACTIVATED ---")
            )
            stripe.api_key = settings.STRIPE_SECRET_KEY

        for i in range(count):
            self.stdout.write(
                f"--- Creating Payout {i + 1} of {count} for '{business.businessName}' ---"
            )

            eligible_bookings = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                status="completed",
                payout_status="pending",
            ).prefetch_related("payments")[:5]

            if not eligible_bookings:
                raise CommandError(
                    f"No eligible bookings found for '{business.businessName}'."
                )

            total_net_amount = Decimal("0.00")
            bookings_for_payout = []
            self.stdout.write("Calculating net payout amount:")
            for b in eligible_bookings:
                payment = b.payments.filter(
                    status__in=["succeeded", "partially_refunded"]
                ).first()
                if not payment:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  - Skipping Booking {b.id}: No successful payment record found."
                        )
                    )
                    continue

                service_fee = payment.service_fee_amount or Decimal("0.00")
                net_amount = b.amount_paid - service_fee
                total_net_amount += net_amount
                bookings_for_payout.append(b)
                self.stdout.write(
                    f"  - Booking {b.id}: Gross ${b.amount_paid}, Fee ${service_fee}, Net ${net_amount}"
                )

            if total_net_amount <= 0:
                raise CommandError(
                    "Total net amount for payout is zero or less. Halting."
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Total Net Amount to be Transferred: ${total_net_amount:.2f}"
                )
            )

            if use_real_stripe:
                self.execute_real_stripe_transfer(
                    business, total_net_amount, bookings_for_payout
                )
            else:
                self.create_fake_payout_record(
                    business, total_net_amount, bookings_for_payout, forced_status
                )

    def execute_real_stripe_transfer(self, business, total_net_amount, bookings):
        try:
            amount_in_cents = int(total_net_amount * 100)
            self.stdout.write(
                f"Attempting to transfer {total_net_amount:.2f} {business.currency} to Stripe Account {business.stripe_account_id}..."
            )

            transfer = stripe.Transfer.create(
                amount=amount_in_cents,
                currency=business.currency or "usd",
                destination=business.stripe_account_id,
                description=f"Payout for {business.businessName}",
                metadata={
                    "source": "management_command",
                    "booking_count": len(bookings),
                },
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"Stripe Transfer {transfer.id} initiated successfully."
                )
            )

            # --- NEW LOGGING BLOCK ---
            self.stdout.write(
                self.style.HTTP_INFO("\n--- Full Stripe API Response ---")
            )
            # Pretty-print the entire Stripe object to the console
            self.stdout.write(json.dumps(transfer, indent=2))
            self.stdout.write(
                self.style.HTTP_INFO("--------------------------------\n")
            )
            # --- END OF LOGGING BLOCK ---

            arrival_timestamp = transfer.get("arrival_date")
            arrival_date_obj = (
                timezone.datetime.fromtimestamp(arrival_timestamp).date()
                if arrival_timestamp
                else None
            )
            transfer_status = transfer.get("status", "pending")

            payout = Payout.objects.create(
                business=business,
                stripe_transfer_id=transfer.id,
                amount=Decimal(transfer.amount) / 100,
                currency=transfer.currency,
                arrival_date=arrival_date_obj,
                status=transfer_status,
                created_at=timezone.datetime.fromtimestamp(transfer.created),
                metadata=transfer.get("metadata", {}),
            )

            booking_ids = [b.id for b in bookings]
            payout.bookings.set(bookings)
            Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")

            self.stdout.write(
                self.style.SUCCESS(
                    f"Local Payout record {payout.id} created with status '{transfer_status}'.\n"
                )
            )

        except stripe.StripeError as e:
            raise CommandError(f"Stripe API Error: {e}")
        except Exception as e:
            raise CommandError(
                f"An unexpected error occurred after the Stripe API call: {e}"
            )

    def create_fake_payout_record(
        self, business, total_net_amount, bookings, forced_status
    ):
        self.stdout.write("Creating fake local payout record (no real transaction).")

        status_choice = forced_status or random.choice(
            ["paid", "pending", "in_transit", "failed"]
        )
        created_date = timezone.now() - timedelta(days=random.randint(1, 30))
        arrival_date = created_date.date() + timedelta(days=random.randint(2, 5))

        payout = Payout.objects.create(
            business=business,
            stripe_transfer_id=f"tr_fake_{random.randint(10000, 99999)}",
            amount=total_net_amount,
            currency=business.currency or "USD",
            arrival_date=arrival_date,
            status=status_choice,
            created_at=created_date,
            metadata={"source": "test_data_command", "type": "fake"},
        )

        booking_ids = [b.id for b in bookings]
        payout.bookings.set(bookings)
        Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully created FAKE Payout {payout.id} with status: {status_choice.upper()}.\n"
            )
        )

    def delete_test_payouts(self, business):
        payouts_to_delete = Payout.objects.filter(
            business=business,
            metadata__source__in=["test_data_command", "management_command"],
        )

        if not payouts_to_delete.exists():
            self.stdout.write(
                f"No command-generated payouts found for '{business.businessName}' to delete."
            )
            return

        count = payouts_to_delete.count()
        self.stdout.write(
            f"Found {count} payout(s) for '{business.businessName}'. Deleting..."
        )

        for payout in payouts_to_delete:
            payout.bookings.all().update(payout_status="pending")

        payouts_to_delete.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully deleted {count} payout(s) and reset associated bookings."
            )
        )
