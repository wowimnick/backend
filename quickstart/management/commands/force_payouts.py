from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import stripe
import pytz
import random

from quickstart.models import Booking, Payout, Payment, PartnerTier

# Define HST Rate for Ontario directly in the command for consistency
HST_RATE = Decimal("0.13")


class Command(BaseCommand):
    help = (
        "!!! FOR TESTING ONLY !!! Instantly marks ALL confirmed bookings (including future ones) "
        'as "completed" and immediately processes their payouts.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--business_id",
            type=int,
            help="Optional: Process payouts only for a specific business ID.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required confirmation to run this potentially destructive command.",
        )

    def handle(self, *args, **options):
        business_id = options.get("business_id")

        self.stdout.write(
            self.style.ERROR(
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            )
        )
        self.stdout.write(
            self.style.ERROR(
                "!!! WARNING: DESTRUCTIVE TESTING COMMAND                  !!!"
            )
        )
        self.stdout.write(
            self.style.ERROR(
                "!!! This script will mark ALL confirmed bookings, including !!!"
            )
        )
        self.stdout.write(
            self.style.ERROR(
                "!!! FUTURE bookings, as 'completed' and trigger payouts.  !!!"
            )
        )
        self.stdout.write(
            self.style.ERROR(
                "!!! DO NOT RUN THIS IN A PRODUCTION ENVIRONMENT.            !!!"
            )
        )
        self.stdout.write(
            self.style.ERROR(
                "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
            )
        )

        if not options["confirm"]:
            raise CommandError(
                "Confirmation not provided. Please re-run the command with the --confirm flag to proceed."
            )

        self.stdout.write(
            self.style.NOTICE(
                "\n--- Starting Instant Payout Process (All Bookings Mode) ---"
            )
        )
        if business_id:
            self.stdout.write(
                self.style.NOTICE(
                    f"--- Targeting specific Business ID: {business_id} ---"
                )
            )

        # --- STEP 1: Mark ALL Confirmed Bookings as 'Completed' ---
        self.stdout.write(
            "Step 1: Finding and marking ALL confirmed bookings as 'completed'..."
        )

        bookings_to_update_qs = Booking.objects.filter(status="confirmed")

        if business_id:
            bookings_to_update_qs = bookings_to_update_qs.filter(
                schedule_instance__schedule__option__classId__businessId=business_id
            )

        updated_count = bookings_to_update_qs.update(status="completed")
        self.stdout.write(
            self.style.SUCCESS(
                f"Step 1 Complete: Marked {updated_count} bookings as 'completed'."
            )
        )

        # --- STEP 2: Process Payouts for 'Completed' Bookings ---
        self.stdout.write("\nStep 2: Calculating and processing payouts...")

        bookings_to_payout_qs = (
            Booking.objects.filter(
                status="completed",
                payment_status="paid",
                payout_status="pending",
            )
            .select_related("schedule_instance__schedule__option__classId__businessId")
            .prefetch_related("payments")
        )

        if business_id:
            bookings_to_payout_qs = bookings_to_payout_qs.filter(
                schedule_instance__schedule__option__classId__businessId=business_id
            )

        if not bookings_to_payout_qs.exists():
            self.stdout.write(
                self.style.SUCCESS(
                    "Step 2 Complete: No bookings found requiring payout."
                )
            )
            return

        payouts_by_business = {}
        for booking in bookings_to_payout_qs:
            business = booking.schedule_instance.schedule.option.classId.businessId
            if business.stripe_account_id:
                payouts_by_business.setdefault(
                    business.stripe_account_id,
                    {
                        "business_instance": business,
                        "total_payout": Decimal("0.0"),
                        "booking_ids": [],
                    },
                )
                payment = booking.payments.filter(status="succeeded").first()
                if payment:
                    payouts_by_business[business.stripe_account_id][
                        "total_payout"
                    ] += payment.net_payout_amount
                    payouts_by_business[business.stripe_account_id][
                        "booking_ids"
                    ].append(booking.id)
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Booking {booking.id} skipped: no associated successful payment record found."
                        )
                    )

        self.stdout.write(
            f"Found {len(payouts_by_business)} businesses to process payouts for."
        )

        successful_payouts = 0
        failed_payouts = 0

        for stripe_id, data in payouts_by_business.items():
            business = data["business_instance"]
            net_payout_amount = data["total_payout"]
            booking_ids = data["booking_ids"]

            if net_payout_amount <= Decimal("0.50"):
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping payout for Business {business.businessId}: amount ${net_payout_amount} is too low."
                    )
                )
                continue

            payout_record = None
            temp_transfer_id = f"temp_cmd_{timezone.now().strftime('%Y%m%d%H%M%S')}_{business.businessId}"

            try:
                with transaction.atomic():
                    payout_amount_cents = int(net_payout_amount * 100)
                    payout_record = Payout.objects.create(
                        business=business,
                        stripe_transfer_id=temp_transfer_id,
                        amount=net_payout_amount,
                        currency=business.currency.upper(),
                        arrival_date=timezone.now().date()
                        + timedelta(days=3),  # Default value
                        status="pending",
                        metadata={
                            "source": "manual_force_payout_command_TESTING",
                            "admin_user": "system_command",
                            "business_id": business.businessId,
                            "booking_count": len(booking_ids),
                        },
                    )

                    transfer = stripe.Transfer.create(
                        amount=payout_amount_cents,
                        currency=business.currency.lower(),
                        destination=stripe_id,
                        description=f"ClassEasily Payout (Forced Manual Trigger)",
                        metadata={
                            "business_id": business.businessId,
                            "booking_count": len(booking_ids),
                            "payout_record_id": str(payout_record.id),
                        },
                    )

                    # --- FIX: Safely access 'arrival_date' using getattr ---
                    # This prevents an AttributeError if the key is missing in the Stripe response.
                    arrival_timestamp = getattr(transfer, "arrival_date", None)
                    if arrival_timestamp:
                        arrival_date = datetime.fromtimestamp(
                            arrival_timestamp, tz=pytz.utc
                        ).date()
                    else:
                        # If Stripe doesn't provide an estimate, set a default
                        arrival_date = timezone.now().date() + timedelta(days=3)

                    payout_record.stripe_transfer_id = transfer.id
                    payout_record.arrival_date = arrival_date
                    payout_record.status = "paid"
                    payout_record.metadata.update(transfer.metadata)
                    payout_record.save()

                    bookings = Booking.objects.filter(id__in=booking_ids)
                    payout_record.bookings.set(bookings)
                    bookings.update(payout_status="processed")

                    successful_payouts += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  -> SUCCESS: Created Stripe Transfer {transfer.id} for Business {business.businessId}."
                        )
                    )

            except stripe.error.StripeError as stripe_error:
                failed_payouts += 1
                if payout_record and payout_record.pk:  # Check if record was saved
                    payout_record.status = "failed"
                    payout_record.save(update_fields=["status"])
                self.stdout.write(
                    self.style.ERROR(
                        f"  -> STRIPE ERROR for Business {business.businessId}: {stripe_error}"
                    )
                )
            except Exception as general_error:
                failed_payouts += 1
                if payout_record and payout_record.pk:  # Check if record was saved
                    payout_record.status = "failed"
                    payout_record.save(update_fields=["status"])
                self.stderr.write(
                    self.style.ERROR(
                        f"  -> GENERAL ERROR for Business {business.businessId}: {general_error}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nStep 2 Complete. Successful payouts: {successful_payouts}. Failed payouts: {failed_payouts}."
            )
        )
        self.stdout.write(self.style.NOTICE("--- Instant Payout Process Finished ---"))
