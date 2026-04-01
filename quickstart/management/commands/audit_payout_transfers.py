"""
Read-only audit: cross-check Django Payout / Booking data against Stripe transfer logs.

Example:
  python manage.py audit_payout_transfers --days 45
  python manage.py audit_payout_transfers --days 45 --from-stripe --limit 50
  python manage.py audit_payout_transfers --stripe-destination acct_1S6HLdFq5ncnBV6Q
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone as py_timezone
from decimal import Decimal

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from quickstart.models import Booking, BusinessInfo, Payout

stripe.api_key = settings.STRIPE_SECRET_KEY


class Command(BaseCommand):
    help = (
        "Print payout records, linked bookings, pending payout queues, and optional "
        "Stripe Transfer.list() for cross-checking against the Stripe Dashboard (read-only)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=60,
            help="Include Payout rows with created_at within this many days (default: 60).",
        )
        parser.add_argument(
            "--stripe-destination",
            action="append",
            default=[],
            metavar="ACCT_ID",
            help="Only payouts for businesses with this Connect account id (repeatable).",
        )
        parser.add_argument(
            "--business-id",
            type=int,
            default=None,
            help="Filter to a single BusinessInfo.businessId.",
        )
        parser.add_argument(
            "--from-stripe",
            action="store_true",
            help="Also call Stripe Transfer.list (newest first) for comparison.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Max transfers to fetch from Stripe when --from-stripe is set (default: 100).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dest_filters = [d.strip() for d in (options["stripe_destination"] or []) if d.strip()]
        business_id = options["business_id"]
        from_stripe = options["from_stripe"]
        limit = max(1, min(options["limit"], 500))

        key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
        key_kind = "unknown"
        if key.startswith("sk_live"):
            key_kind = "LIVE"
        elif key.startswith("sk_test"):
            key_kind = "TEST"

        self.stdout.write("=" * 80)
        self.stdout.write("PAYOUT TRANSFER AUDIT (read-only, no DB writes)")
        self.stdout.write(f"Server time (Django): {timezone.now().isoformat()}")
        self.stdout.write(f"Stripe API key mode: {key_kind}")
        self.stdout.write(f"Payout window: last {days} days")
        if dest_filters:
            self.stdout.write(f"Connect filter: {dest_filters}")
        if business_id is not None:
            self.stdout.write(f"Business filter: businessId={business_id}")
        self.stdout.write("=" * 80)

        since = timezone.now() - timedelta(days=days)

        qs = Payout.objects.filter(created_at__gte=since).select_related("business")
        if business_id is not None:
            qs = qs.filter(business_id=business_id)
        if dest_filters:
            qs = qs.filter(business__stripe_account_id__in=dest_filters)

        payouts = list(qs.order_by("-created_at").prefetch_related("bookings"))

        # --- Summary by business + day + amount (duplicate detector) ---
        bucket = defaultdict(list)
        for p in payouts:
            day = p.created_at.date().isoformat()
            dest = p.business.stripe_account_id or ""
            key_b = (p.business_id, str(p.amount), p.currency.upper(), day, dest)
            bucket[key_b].append(p)

        dup_groups = {k: v for k, v in bucket.items() if len(v) > 1}
        if dup_groups:
            self.stdout.write("\n*** DUPLICATE-LIKE PAYOUT ROWS (same business, amount, currency, day, destination) ***")
            for (bid, amt, cur, day, dest), plist in sorted(
                dup_groups.items(), key=lambda x: (x[0][3], x[0][0]), reverse=True
            ):
                self.stdout.write(
                    f"  business_id={bid} amount={amt} {cur} day={day} destination={dest} count={len(plist)}"
                )
                for p in plist:
                    self.stdout.write(
                        f"    payout_id={p.id} stripe_transfer_id={p.stripe_transfer_id} status={p.status} created_at={p.created_at.isoformat()}"
                    )
            self.stdout.write("")
        else:
            self.stdout.write("\n(no duplicate-like payout rows in this window by day/amount/destination)\n")

        # --- Each payout record (detail) ---
        self.stdout.write("-" * 80)
        self.stdout.write(f"DATABASE: {len(payouts)} Payout row(s) in window")
        self.stdout.write("-" * 80)

        for p in payouts:
            b = p.business
            bookings = list(p.bookings.all().order_by("id"))
            bid_list = [bk.id for bk in bookings]
            sum_alloc = sum(
                (bk.allocated_net_payout or Decimal("0") for bk in bookings),
                start=Decimal("0"),
            )
            temp_flag = (p.metadata or {}).get("temp_id") if isinstance(p.metadata, dict) else None

            self.stdout.write("")
            self.stdout.write(f"Payout DB id:        {p.id}")
            self.stdout.write(f"stripe_transfer_id:  {p.stripe_transfer_id}")
            if str(p.stripe_transfer_id).startswith("temp_"):
                self.stdout.write(self.style.WARNING("  ^ WARNING: still looks like a TEMP id (transfer may have failed before update)"))
            self.stdout.write(f"amount / currency:   {p.amount} {p.currency.upper()}")
            self.stdout.write(f"status:              {p.status}")
            self.stdout.write(f"arrival_date:        {p.arrival_date}")
            self.stdout.write(f"created_at (DB):     {p.created_at.isoformat()}")
            self.stdout.write(
                f"business:            id={b.businessId} name={getattr(b, 'businessName', '')!r}"
            )
            self.stdout.write(f"stripe_account_id:   {b.stripe_account_id or '(none)'}")
            self.stdout.write(f"metadata.temp_id:    {temp_flag!r}")
            self.stdout.write(f"metadata (full):     {json.dumps(p.metadata, default=str)}")
            self.stdout.write(
                f"linked bookings:     count={len(bookings)} sum_allocated_net_payout={sum_alloc} ids={bid_list}"
            )
            for bk in bookings[:25]:
                inst = getattr(bk, "schedule_instance", None)
                sess = ""
                if inst:
                    sess = f"{inst.date} {getattr(inst, 'time', '')}"
                self.stdout.write(
                    f"    booking id={bk.id} ref={getattr(bk, 'user_facing_reference', '')!r} "
                    f"payout_status={bk.payout_status} allocated_net_payout={bk.allocated_net_payout} session={sess}"
                )
            if len(bookings) > 25:
                self.stdout.write(f"    ... {len(bookings) - 25} more bookings not shown")

        # --- Pending payout queue (what the nightly job would still pay) ---
        self.stdout.write("\n" + "-" * 80)
        self.stdout.write("DATABASE: bookings still payout_status=pending (completed|forfeited, paid)")
        self.stdout.write("-" * 80)

        pending_qs = Booking.objects.filter(
            status__in=["completed", "forfeited"],
            payment_status="paid",
            payout_status="pending",
        ).select_related(
            "schedule_instance__schedule__option__classId__businessId"
        )
        if business_id is not None:
            pending_qs = pending_qs.filter(
                schedule_instance__schedule__option__classId__businessId=business_id
            )
        if dest_filters:
            pending_qs = pending_qs.filter(
                schedule_instance__schedule__option__classId__businessId__stripe_account_id__in=dest_filters
            )

        agg = pending_qs.values(
            "schedule_instance__schedule__option__classId__businessId"
        ).annotate(
            c=Count("id"),
            total_alloc=Sum("allocated_net_payout"),
        )
        rows = list(agg.order_by("-total_alloc"))
        if not rows:
            self.stdout.write("(none)")
        else:
            for row in rows:
                bid = row["schedule_instance__schedule__option__classId__businessId"]
                try:
                    biz = BusinessInfo.objects.get(pk=bid)
                    acct = biz.stripe_account_id or ""
                    name = getattr(biz, "businessName", "")
                except BusinessInfo.DoesNotExist:
                    acct = "?"
                    name = "?"
                total_alloc = row["total_alloc"] or Decimal("0")
                self.stdout.write(
                    f"  businessId={bid} name={name!r} stripe_account_id={acct} "
                    f"pending_bookings={row['c']} sum_allocated_net_payout={total_alloc}"
                )

        # --- Stripe API (optional) ---
        if from_stripe:
            self.stdout.write("\n" + "-" * 80)
            self.stdout.write(f"STRIPE API: Transfer.list(limit={limit})")
            self.stdout.write("-" * 80)
            try:
                tr_list = stripe.Transfer.list(limit=limit)
                db_transfer_ids = {p.stripe_transfer_id for p in payouts if p.stripe_transfer_id}
                seen = []
                for tr in getattr(tr_list, "data", []) or []:
                    tid = getattr(tr, "id", "")
                    dest = getattr(tr, "destination", "") or ""
                    amt = getattr(tr, "amount", None)
                    cur = (getattr(tr, "currency", "") or "").upper()
                    desc = getattr(tr, "description", "") or ""
                    created = getattr(tr, "created", None)
                    created_s = ""
                    if created is not None:
                        created_s = datetime.fromtimestamp(
                            int(created), tz=py_timezone.utc
                        ).isoformat()
                    dollars = ""
                    if amt is not None:
                        try:
                            dollars = str(Decimal(amt) / Decimal(100))
                        except Exception:
                            dollars = str(amt)
                    line = (
                        f"id={tid} amount={dollars} {cur} destination={dest} "
                        f"description={desc!r} created={created_s}"
                    )
                    self.stdout.write(line)
                    seen.append(tid)
                    if tid and tid not in db_transfer_ids:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  ^ NO matching Payout.stripe_transfer_id in audited DB window (may be outside --days or never saved)"
                            )
                        )

                # DB ids that look like real tr_ but not in this Stripe page
                stripe_page = set(seen)
                missing_on_page = [
                    p.stripe_transfer_id
                    for p in payouts
                    if p.stripe_transfer_id
                    and str(p.stripe_transfer_id).startswith("tr_")
                    and p.stripe_transfer_id not in stripe_page
                ]
                if missing_on_page and len(missing_on_page) <= 30:
                    self.stdout.write("\nDB payout stripe_transfer_id (tr_) not in this Stripe list page:")
                    for x in missing_on_page[:30]:
                        self.stdout.write(f"  {x}")
                elif missing_on_page:
                    self.stdout.write(
                        f"\n({len(missing_on_page)} tr_ ids in DB not on this Stripe page — increase --limit or narrow --days)"
                    )

            except stripe.StripeError as e:
                self.stdout.write(self.style.ERROR(f"Stripe API error: {e}"))

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write("Done.")
        self.stdout.write("=" * 80)
