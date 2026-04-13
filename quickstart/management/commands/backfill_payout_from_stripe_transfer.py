"""
When a Stripe Connect transfer succeeded but the Payout row was never committed
(e.g. task crashed after Transfer.create), use this to align the database with Stripe
so process_daily_payouts will not pay those bookings again.

Does not call Stripe to move money — only retrieves the transfer to verify id/amount/destination.

Example (Pottery Dream — keep oldest transfer as the recorded payout):
  python manage.py backfill_payout_from_stripe_transfer \\
    --business-id 45 \\
    --stripe-transfer-id tr_1TGA32C4fwivxhVmm0iK91u7 \\
    --dry-run

Then run without --dry-run after confirming output.
"""

from decimal import Decimal

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, Payout

stripe.api_key = settings.STRIPE_SECRET_KEY


def _pending_positive_net_booking_ids(business) -> tuple[list[int], Decimal, Decimal]:
    """Same rules as process_daily_payouts (allocated_net_payout includes Stripe fee)."""
    qs = (
        Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business,
            status__in=["completed", "forfeited"],
            payment_status="paid",
            payout_status="pending",
        )
        .prefetch_related("payments")
        .order_by("id")
    )
    booking_ids: list[int] = []
    total_payout = Decimal("0.00")
    total_stripe_fees = Decimal("0.00")
    for booking in qs:
        allocated = booking.allocated_net_payout or Decimal("0.00")
        if allocated <= 0:
            continue
        payment = next((p for p in booking.payments.all() if p.status == "succeeded"), None)
        if payment and payment.stripe_processing_fee:
            total_stripe_fees += payment.stripe_processing_fee
        total_payout += allocated
        booking_ids.append(booking.id)
    return booking_ids, total_payout, total_stripe_fees


class Command(BaseCommand):
    help = (
        "Create Payout row for an existing Stripe transfer and mark pending bookings processed "
        "(recovery from missing DB commit after transfer)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--business-id", type=int, required=True)
        parser.add_argument("--stripe-transfer-id", type=str, required=True)
        parser.add_argument(
            "--booking-ids",
            type=str,
            default="",
            help="Comma-separated booking ids to attach. Default: all pending positive-net bookings for the business.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show actions only; no database writes.",
        )
        parser.add_argument(
            "--skip-amount-check",
            action="store_true",
            help="Do not verify transfer amount matches model net total (use with care).",
        )
        parser.add_argument(
            "--tolerance",
            type=str,
            default="0.02",
            help="Max dollar difference allowed between Stripe transfer and model total (default 0.02).",
        )

    def handle(self, *args, **options):
        business_id = options["business_id"]
        transfer_id = (options["stripe_transfer_id"] or "").strip()
        dry_run = options["dry_run"]
        skip_amount_check = options["skip_amount_check"]
        tol = Decimal(options["tolerance"])

        if not transfer_id.startswith("tr_"):
            raise CommandError(f"stripe_transfer_id must look like tr_... got {transfer_id!r}")

        try:
            business = BusinessInfo.objects.get(pk=business_id)
        except BusinessInfo.DoesNotExist as e:
            raise CommandError(f"Business id={business_id} not found.") from e

        if Payout.objects.filter(stripe_transfer_id=transfer_id).exists():
            raise CommandError(
                f"Payout with stripe_transfer_id={transfer_id} already exists; nothing to backfill."
            )

        raw_booking = (options["booking_ids"] or "").strip()
        if raw_booking:
            try:
                booking_ids = [int(x.strip()) for x in raw_booking.split(",") if x.strip()]
            except ValueError as e:
                raise CommandError(f"Invalid --booking-ids: {raw_booking!r}") from e
            if not booking_ids:
                raise CommandError("--booking-ids was empty after parsing.")
            # Recompute total only for those bookings, same fee rules
            bookings = list(
                Booking.objects.filter(
                    id__in=booking_ids,
                    schedule_instance__schedule__option__classId__businessId=business,
                    status__in=["completed", "forfeited"],
                    payment_status="paid",
                    payout_status="pending",
                ).prefetch_related("payments")
            )
            found = {b.id for b in bookings}
            missing = set(booking_ids) - found
            if missing:
                raise CommandError(
                    f"Bookings not found, wrong business, or not eligible (pending paid completed|forfeited): {sorted(missing)}"
                )
            total_payout = Decimal("0.00")
            total_stripe_fees = Decimal("0.00")
            for booking in sorted(bookings, key=lambda b: b.id):
                allocated = booking.allocated_net_payout or Decimal("0.00")
                if allocated <= 0:
                    raise CommandError(f"Booking {booking.id} has non-positive allocated_net_payout.")
                payment = next((p for p in booking.payments.all() if p.status == "succeeded"), None)
                if payment and payment.stripe_processing_fee:
                    total_stripe_fees += payment.stripe_processing_fee
                total_payout += allocated
        else:
            booking_ids, total_payout, total_stripe_fees = _pending_positive_net_booking_ids(business)

        if not booking_ids:
            raise CommandError("No eligible pending bookings with positive net payout for this business.")

        try:
            tr = stripe.Transfer.retrieve(transfer_id)
        except stripe.error.StripeError as e:
            raise CommandError(f"Stripe retrieve failed: {e}") from e

        dest = getattr(tr, "destination", None) or ""
        if business.stripe_account_id and dest and business.stripe_account_id != dest:
            raise CommandError(
                f"Transfer destination {dest!r} does not match business.stripe_account_id "
                f"{business.stripe_account_id!r}."
            )

        amount_cents = int(getattr(tr, "amount", 0) or 0)
        stripe_amount = (Decimal(amount_cents) / Decimal(100)).quantize(Decimal("0.01"))
        currency = (getattr(tr, "currency", None) or business.currency or "cad").upper()

        if not skip_amount_check:
            diff = abs(stripe_amount - total_payout)
            if diff > tol:
                raise CommandError(
                    f"Amount mismatch: Stripe transfer {stripe_amount} {currency} vs model total "
                    f"{total_payout} {currency} (diff {diff}, tolerance {tol}). "
                    f"Fix booking selection or use --skip-amount-check if intentional."
                )

        self.stdout.write(f"Business: {business.businessName} (id={business.businessId})")
        self.stdout.write(f"Stripe transfer: {transfer_id} amount={stripe_amount} {currency} dest={dest}")
        self.stdout.write(f"Bookings to mark processed: {booking_ids}")
        self.stdout.write(f"Model net total (after est. Stripe fees): {total_payout} {currency}")

        meta = {
            "source": "backfill_payout_from_stripe_transfer",
            "skip_payout_notification": True,
            "business_id": business.businessId,
            "booking_count": len(booking_ids),
            "booking_ids": [str(i) for i in booking_ids],
            "stripe_fees_deducted": str(total_stripe_fees),
            "backfilled_at": timezone.now().isoformat(),
        }

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no rows written."))
            return

        with transaction.atomic():
            payout = Payout.objects.create(
                business=business,
                stripe_transfer_id=transfer_id,
                amount=stripe_amount,
                currency=currency,
                arrival_date=timezone.now().date(),
                status="paid",
                metadata=meta,
            )
            Booking.objects.filter(id__in=booking_ids).update(payout_status="processed")
            payout.bookings.add(*booking_ids)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Payout pk={payout.id}, linked bookings {booking_ids}, status=paid."
            )
        )
