"""
Recalculate platform fee and net payout for a business's unpaid payouts after a tier correction.

Use when a business was on the wrong partner tier (e.g. 0% or lower) and received bookings
with no/lower commission. After setting the business to the correct tier, run this to
update Payment and Booking records so the next payout reflects the correct commission.

Only affects payments that are succeeded and whose bookings have payout_status='pending'
(not yet included in a payout). Does not change the amount charged to the customer.
"""
from decimal import Decimal, ROUND_HALF_UP
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from quickstart.models import BusinessInfo, Payment, PartnerTier

HST_RATE = Decimal("0.13")


class Command(BaseCommand):
    help = (
        "Recalculate platform_fee_amount and net_payout_amount for a business's "
        "pending payments using the business's current partner tier. Use after correcting "
        "a business's tier so future payouts deduct the right commission."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "business",
            type=str,
            help="Business slug (e.g. my-studio) or businessId (integer).",
        )
        parser.add_argument(
            "--fee-percentage",
            type=float,
            default=None,
            help="Override fee percentage (e.g. 13 for 13%%). Default: use business's current partner_tier.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without saving.",
        )

    def handle(self, *args, **options):
        business_identifier = options["business"].strip()
        fee_override = options.get("fee_percentage")
        dry_run = options["dry_run"]

        # Resolve business
        business = self._get_business(business_identifier)
        if not business:
            raise CommandError(
                f"Business not found: '{business_identifier}'. "
                "Use slug (e.g. my-studio) or businessId (integer)."
            )

        # Fee percentage: override or from current partner_tier
        if fee_override is not None:
            if fee_override < 0 or fee_override > 100:
                raise CommandError("--fee-percentage must be between 0 and 100.")
            fee_percentage = Decimal(str(fee_override))
            self.stdout.write(
                self.style.NOTICE(
                    f"Using fee percentage: {fee_percentage}% (override)."
                )
            )
        else:
            if business.partner_tier_id is None:
                try:
                    default_tier = PartnerTier.objects.get(is_default=True)
                    fee_percentage = default_tier.fee_percentage
                    self.stdout.write(
                        self.style.NOTICE(
                            f"Business has no partner_tier; using default tier "
                            f"'{default_tier.name}' ({fee_percentage}%)."
                        )
                    )
                except PartnerTier.DoesNotExist:
                    raise CommandError(
                        "Business has no partner_tier and no default PartnerTier exists. "
                        "Set the business's tier or use --fee-percentage."
                    )
            else:
                fee_percentage = business.partner_tier.fee_percentage
                self.stdout.write(
                    self.style.NOTICE(
                        f"Using current partner tier '{business.partner_tier.name}': "
                        f"{fee_percentage}%."
                    )
                )

        service_fee_rate = fee_percentage / Decimal("100")

        # Payments for this business that are succeeded and not yet paid out
        payments = (
            Payment.objects.filter(
                booking__schedule_instance__schedule__option__classId__businessId=business,
                status="succeeded",
                booking__payout_status="pending",
            )
            .select_related("booking")
            .order_by("id")
        )

        if not payments.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"No pending (unpaid) succeeded payments found for "
                    f"{business.businessName} (slug={business.slug})."
                )
            )
            return

        total_platform_fee_delta = Decimal("0.00")
        total_net_payout_delta = Decimal("0.00")
        updated_count = 0

        for payment in payments:
            # Recover pre-tax subtotal: amount = subtotal + platform_fee + tax
            tax_amount = payment.tax_amount or Decimal("0.00")
            subtotal_for_payout = (
                payment.amount - tax_amount - payment.platform_fee_amount
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if subtotal_for_payout <= 0:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skip Payment id={payment.id} (booking={payment.booking_id}): "
                        "subtotal_for_payout <= 0."
                    )
                )
                continue

            new_platform_fee = (
                (subtotal_for_payout * service_fee_rate).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
            new_platform_fee_tax = (
                (new_platform_fee * HST_RATE).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
            business_payout_tax = tax_amount - new_platform_fee_tax
            new_net_payout = (
                (subtotal_for_payout - new_platform_fee + business_payout_tax).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )

            platform_fee_delta = new_platform_fee - payment.platform_fee_amount
            net_payout_delta = new_net_payout - payment.net_payout_amount

            self.stdout.write(
                f"  Payment id={payment.id} booking={payment.booking_id}: "
                f"platform_fee {payment.platform_fee_amount} -> {new_platform_fee}, "
                f"net_payout {payment.net_payout_amount} -> {new_net_payout}"
            )

            if not dry_run:
                with transaction.atomic():
                    payment.platform_fee_amount = new_platform_fee
                    payment.platform_fee_tax = new_platform_fee_tax
                    payment.net_payout_amount = new_net_payout
                    payment.save(
                        update_fields=[
                            "platform_fee_amount",
                            "platform_fee_tax",
                            "net_payout_amount",
                        ]
                    )
                    booking = payment.booking
                    booking.allocated_net_payout = new_net_payout
                    booking.save(update_fields=["allocated_net_payout"])

            total_platform_fee_delta += platform_fee_delta
            total_net_payout_delta += net_payout_delta
            updated_count += 1

        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY RUN: would update {updated_count} payment(s). "
                    "Run without --dry-run to apply."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated {updated_count} payment(s) and their bookings."
                )
            )
        self.stdout.write(
            f"  Total platform fee change: {total_platform_fee_delta:+.2f}"
        )
        self.stdout.write(
            f"  Total net payout change:  {total_net_payout_delta:+.2f}"
        )

    def _get_business(self, identifier):
        """Return BusinessInfo by slug or by businessId (integer)."""
        if identifier.isdigit():
            try:
                return BusinessInfo.objects.get(businessId=int(identifier))
            except BusinessInfo.DoesNotExist:
                return None
        try:
            return BusinessInfo.objects.get(slug=identifier)
        except BusinessInfo.DoesNotExist:
            return None
