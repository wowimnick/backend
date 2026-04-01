"""
Read-only: orphan Stripe transfers (no Payout.stripe_transfer_id) with booking-level context,
estimated payout totals (same fee model as daily task), and suggested keep/reverse splits.

Example:
  python manage.py reconcile_orphan_transfers --days 120
  python manage.py reconcile_orphan_transfers --days 90 --destination acct_1S6HLdFq5ncnBV6Q
"""

import json
import uuid as uuid_lib
from collections import defaultdict
from datetime import datetime, timedelta, timezone as py_timezone
from decimal import Decimal

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from quickstart.models import Booking, BusinessInfo, Payout
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict
from quickstart.utils.stripe_transfer_reversal import merge_reversal_into_orphan

stripe.api_key = settings.STRIPE_SECRET_KEY

MAX_PAGES = 40
PAGE_SIZE = 100

# Match process_daily_payouts fee estimate (2.9% + $0.30 per charge).
STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_FIXED = Decimal("0.30")


def _estimate_stripe_processing_fee(charge_amount):
    if charge_amount is None or charge_amount <= 0:
        return Decimal("0.00")
    fee = (charge_amount * STRIPE_FEE_PERCENT + STRIPE_FEE_FIXED).quantize(Decimal("0.01"))
    return fee


def _expected_transfer_for_business(business_id: int):
    """
    Mirror process_daily_payouts net calculation for pending payout bookings.
    Returns (total_transfer, details, zero_dollar_ids).
    """
    qs = (
        Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business_id,
            status__in=["completed", "forfeited"],
            payment_status="paid",
            payout_status="pending",
        )
        .prefetch_related("payments")
        .order_by("id")
    )

    total = Decimal("0.00")
    details = []
    zero_ids = []

    for booking in qs:
        allocated = booking.allocated_net_payout or Decimal("0.00")
        if allocated <= 0:
            zero_ids.append(booking.id)
            continue
        payment = next(
            (p for p in booking.payments.all() if p.status == "succeeded"),
            None,
        )
        stripe_fee = Decimal("0.00")
        if payment and payment.amount:
            stripe_fee = _estimate_stripe_processing_fee(payment.amount)
        net_after = (allocated - stripe_fee).quantize(Decimal("0.01"))
        row = {
            "booking_id": booking.id,
            "ref": getattr(booking, "user_facing_reference", "") or "",
            "allocated_net_payout": allocated,
            "stripe_fee_est": stripe_fee,
            "net_after_fees": net_after,
        }
        if net_after > 0:
            total += net_after
            details.append(row)
        else:
            zero_ids.append(booking.id)
            row["note"] = "net_after_fees<=0"
            details.append(row)

    return total, details, zero_ids


def _meta_business_id(o):
    bid = (o.get("metadata") or {}).get("business_id")
    try:
        return int(bid) if bid is not None and str(bid).isdigit() else None
    except (TypeError, ValueError):
        return None


def _money_eq(a: Decimal, b: Decimal) -> bool:
    return a.quantize(Decimal("0.01")) == b.quantize(Decimal("0.01"))


def _money_close(a: Decimal, b: Decimal, tol: Decimal = Decimal("1.00")) -> bool:
    return abs(a - b) <= tol


def _collect_orphans(days, dest_filters, desc_filter, db_transfer_ids):
    cutoff = datetime.now(tz=py_timezone.utc) - timedelta(days=max(1, days))
    cutoff_ts = int(cutoff.timestamp())

    orphans = []
    starting_after = None
    pages = 0

    while pages < MAX_PAGES:
        pages += 1
        kwargs = {"limit": PAGE_SIZE}
        if starting_after:
            kwargs["starting_after"] = starting_after
        page = stripe.Transfer.list(**kwargs)
        data = getattr(page, "data", None) or []
        if not data:
            break

        for tr in data:
            created = int(getattr(tr, "created", 0) or 0)
            if created < cutoff_ts:
                continue

            tid = getattr(tr, "id", "") or ""
            if not tid.startswith("tr_"):
                continue

            dest = getattr(tr, "destination", None) or ""
            if dest_filters and dest not in dest_filters:
                continue

            desc = getattr(tr, "description", "") or ""
            if desc_filter and desc_filter not in desc:
                continue

            if tid in db_transfer_ids:
                continue

            amt_cents = getattr(tr, "amount", None)
            cur = (getattr(tr, "currency", "") or "").upper()
            try:
                dollars = (Decimal(int(amt_cents or 0)) / Decimal(100)).quantize(Decimal("0.01"))
            except Exception:
                dollars = Decimal("0.00")

            meta = stripe_metadata_to_dict(getattr(tr, "metadata", None))

            row = {
                "transfer_id": tid,
                "amount": dollars,
                "currency": cur,
                "destination": dest,
                "created": datetime.fromtimestamp(created, tz=py_timezone.utc).isoformat(),
                "created_ts": created,
                "description": desc,
                "metadata": meta,
            }
            merge_reversal_into_orphan(row, tr)
            orphans.append(row)

        if not getattr(page, "has_more", False):
            break
        last_created = int(getattr(data[-1], "created", 0) or 0)
        if last_created < cutoff_ts:
            break
        starting_after = data[-1].id

    return orphans


class Command(BaseCommand):
    help = (
        "Orphan Stripe transfers + pending booking analysis + suggested keep/reverse (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=120,
            help="Stripe transfers created within this many days (default: 120).",
        )
        parser.add_argument(
            "--destination",
            action="append",
            default=[],
            metavar="ACCT_ID",
            help="Only these Connect destinations (repeatable).",
        )
        parser.add_argument(
            "--description-contains",
            type=str,
            default="ClassEasily Payout",
            help="Description filter (default: ClassEasily Payout). Empty string disables.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dest_filters = [d.strip() for d in (options["destination"] or []) if d.strip()]
        desc_filter = (options["description_contains"] or "").strip()

        key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
        key_kind = "LIVE" if key.startswith("sk_live") else "TEST" if key.startswith("sk_test") else "unknown"

        db_transfer_ids = set(
            Payout.objects.exclude(stripe_transfer_id__startswith="temp_")
            .exclude(stripe_transfer_id="")
            .values_list("stripe_transfer_id", flat=True)
        )

        orphans = _collect_orphans(days, dest_filters, desc_filter, db_transfer_ids)

        self.stdout.write("=" * 80)
        self.stdout.write("RECONCILE ORPHAN TRANSFERS (read-only)")
        self.stdout.write(f"Stripe key: {key_kind} | window: {days} days")
        self.stdout.write(f"Known tr_ in Payout table: {len(db_transfer_ids)}")
        self.stdout.write("Fee model: 2.9% + $0.30 per succeeded charge (same as daily payout task)")
        self.stdout.write(
            "Stripe reversal: each transfer shows amount_reversed from the API; "
            "fully_reversed transfers are excluded from suggested reverse/keep."
        )
        self.stdout.write("=" * 80)

        if not orphans:
            self.stdout.write(self.style.SUCCESS("\nNo orphan transfers in this window.\n"))
            return

        cluster = defaultdict(list)
        for o in orphans:
            cluster[(o["destination"], o["amount"], o["currency"])].append(o)

        by_business = defaultdict(list)
        for o in orphans:
            bid_int = _meta_business_id(o)
            by_business[bid_int].append(o)

        for bid in sorted(k for k in by_business.keys() if k is not None):
            self.stdout.write(f"\n{'=' * 80}")
            self.stdout.write(f"BUSINESS ID {bid}")
            try:
                biz = BusinessInfo.objects.get(pk=bid)
                self.stdout.write(
                    f"  name={getattr(biz, 'businessName', '')!r}  stripe_account_id={biz.stripe_account_id!r}"
                )
            except BusinessInfo.DoesNotExist:
                self.stdout.write(self.style.WARNING("  (Business row missing)"))

            exp_total, details, zero_ids = _expected_transfer_for_business(bid)

            self.stdout.write(
                f"\n  CURRENT PENDING PAYOUT QUEUE (completed|forfeited, paid, payout pending):"
            )
            min_note = (
                " (below $0.50 min — daily task would skip a transfer)"
                if exp_total < Decimal("0.50")
                else ""
            )
            self.stdout.write(
                f"  Model expected transfer total (net after est. Stripe fees): {exp_total}{min_note}"
            )
            if zero_ids:
                self.stdout.write(f"  Zero-allocated or non-positive-net booking ids: {zero_ids}")
            if not details:
                self.stdout.write("  (no positive-net booking lines)")
            for d in details:
                self.stdout.write(
                    f"    booking {d['booking_id']} ref={d['ref']!r} "
                    f"allocated={d['allocated_net_payout']} fee_est={d['stripe_fee_est']} "
                    f"net={d['net_after_fees']}"
                )

            for o in sorted(by_business[bid], key=lambda x: x["created_ts"]):
                self._print_orphan_detail(o, exp_total)

        # business_id missing in metadata
        if None in by_business:
            self.stdout.write(f"\n{'=' * 80}")
            self.stdout.write("ORPHANS WITH NO business_id IN METADATA")
            for o in sorted(by_business[None], key=lambda x: x["created_ts"]):
                self._print_orphan_detail(o, None)

        suggested_reverse = []
        suggested_keep = []
        ids_in_multi_cluster = set()

        self.stdout.write(f"\n{'=' * 80}")
        self.stdout.write("CLUSTER SUMMARY (same destination + amount + currency)")
        for (dest, amt, cur), items in sorted(cluster.items(), key=lambda x: str(x)):
            if len(items) >= 2:
                for x in items:
                    ids_in_multi_cluster.add(x["transfer_id"])
            if len(items) < 2:
                continue
            active = [x for x in items if x.get("reversal_label") != "fully_reversed"]
            revd = [x for x in items if x.get("reversal_label") == "fully_reversed"]
            self.stdout.write(f"\n  {len(items)} transfers: {amt} {cur} -> {dest}")
            if revd:
                self.stdout.write(
                    f"    ({len(revd)} fully reversed in Stripe — no balance left on those tr_; "
                    f"{len(active)} still sending funds)"
                )
            bid_int = _meta_business_id(items[0])
            if bid_int is not None:
                exp_total, _, _ = _expected_transfer_for_business(bid_int)
                if exp_total is not None and (
                    _money_eq(amt, exp_total) or _money_close(amt, exp_total, Decimal("2.00"))
                ):
                    if len(active) < 2:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Heuristic skipped: need 2+ non-fully-reversed transfers to suggest "
                                f"duplicate reversals (active={len(active)})."
                            )
                        )
                        continue
                    ordered = sorted(active, key=lambda x: x["created_ts"])
                    keep = ordered[0]["transfer_id"]
                    rev = [x["transfer_id"] for x in ordered[1:]]
                    suggested_keep.append(keep)
                    suggested_reverse.extend(rev)
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Heuristic: model expected transfer {exp_total} aligns with {amt}. "
                            f"KEEP oldest non-reversed {keep}; REVERSE {len(rev)} other(s) still sending funds."
                        )
                    )
                    self.stdout.write(f"    reverse: {' '.join(rev)}")
                elif exp_total is not None and exp_total < Decimal("0.50"):
                    self.stdout.write(
                        self.style.WARNING(
                            f"  Expected model total {exp_total} is below min — queue may be empty/stale; "
                            f"manual review all {len(items)} transfers."
                        )
                    )
                else:
                    self.stdout.write(
                        f"  Expected model total {exp_total} vs cluster amount {amt} — "
                        f"manual review (may need multiple transfers or different bookings per run)."
                    )
            else:
                self.stdout.write("  (no business_id in metadata — manual cluster review)")

        for o in sorted(orphans, key=lambda x: x["created_ts"]):
            if o["transfer_id"] in ids_in_multi_cluster:
                continue
            if o.get("reversal_label") == "fully_reversed":
                continue
            bid_int = _meta_business_id(o)
            if bid_int is None:
                continue
            exp_total, _, _ = _expected_transfer_for_business(bid_int)
            if _money_eq(o["amount"], exp_total) or _money_close(
                o["amount"], exp_total, Decimal("3.00")
            ):
                suggested_keep.append(o["transfer_id"])

        self.stdout.write(f"\n{'=' * 80}")
        self.stdout.write("SUGGESTED REVERSE (heuristic — confirm in Stripe + with host)")
        self.stdout.write(" ".join(sorted(set(suggested_reverse))) if suggested_reverse else "(none)")
        self.stdout.write(f"\n{'=' * 80}")
        self.stdout.write("SUGGESTED KEEP (oldest in each aligned cluster + aligned singles)")
        self.stdout.write(" ".join(sorted(set(suggested_keep))) if suggested_keep else "(none)")
        self.stdout.write(
            "\nHeuristics use the same net-after-fee model as process_daily_payouts; "
            "verify host balance and Stripe before reversing. Not legal/accounting advice.\n"
        )
        self.stdout.write("=" * 80)

    def _print_orphan_detail(self, o, exp_total):
        tid = o["transfer_id"]
        meta = o["metadata"]
        praw = meta.get("payout_record_id")

        self.stdout.write(f"\n  --- Transfer {tid} ---")
        self.stdout.write(
            f"      amount={o['amount']} {o['currency']} created={o['created']} dest={o['destination']}"
        )
        rlab = o.get("reversal_label", "?")
        ar = o.get("amount_reversed_dollars", "?")
        rem = o.get("remaining_dollars", "?")
        self.stdout.write(
            f"      Stripe reversal: {rlab} (reversed={ar} {o.get('currency', '')}, "
            f"net still with connected account={rem} {o.get('currency', '')})"
        )
        if rlab == "fully_reversed":
            self.stdout.write(
                self.style.SUCCESS(
                    "      Funds fully pulled back in Stripe — no reversal action needed on this tr_."
                )
            )
        elif rlab == "partially_reversed":
            self.stdout.write(
                self.style.WARNING(
                    "      Partially reversed — check Stripe for remaining amount to reverse if needed."
                )
            )
        self.stdout.write(f"      metadata: {json.dumps(meta, default=str)}")

        payout_row = None
        if praw:
            try:
                u = uuid_lib.UUID(str(praw))
                payout_row = Payout.objects.filter(pk=u).first()
            except (ValueError, TypeError):
                self.stdout.write(self.style.WARNING(f"      payout_record_id not a valid UUID: {praw!r}"))
        if praw and payout_row:
            self.stdout.write(
                self.style.WARNING(
                    f"      NOTE: Payout row EXISTS for metadata payout_record_id={praw} "
                    f"stripe_transfer_id={payout_row.stripe_transfer_id!r} (unexpected for orphan transfer)"
                )
            )
        elif praw:
            self.stdout.write(
                f"      No Payout DB row for metadata payout_record_id={praw} (rolled back or never committed)."
            )

        if exp_total is not None:
            if _money_eq(o["amount"], exp_total):
                self.stdout.write(
                    self.style.SUCCESS(
                        f"      Amount MATCHES model expected transfer {exp_total} for current pending queue."
                    )
                )
                self.stdout.write(
                    "      If multiple identical transfers exist for this business, keep ONE; reverse duplicates."
                )
            elif _money_close(o["amount"], exp_total, Decimal("3.00")):
                self.stdout.write(
                    self.style.WARNING(
                        f"      Amount CLOSE to model expected {exp_total} (within $3) — may be valid single payout."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"      Amount {o['amount']} vs model expected {exp_total} — review before reversing."
                    )
                )
