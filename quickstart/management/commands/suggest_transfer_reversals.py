"""
Read-only: list Stripe transfer IDs that have NO matching Payout row in the database.

These are the usual candidates to reverse in Stripe after the task failed post-transfer
(but verify amounts / host balance before reversing).

Example:
  python manage.py suggest_transfer_reversals --days 90
  python manage.py suggest_transfer_reversals --days 30 --destination acct_1S6HLdFq5ncnBV6Q
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone as py_timezone
from decimal import Decimal

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from quickstart.models import Payout
from quickstart.utils.stripe_metadata import stripe_metadata_to_dict
from quickstart.utils.stripe_transfer_reversal import merge_reversal_into_orphan

stripe.api_key = settings.STRIPE_SECRET_KEY

MAX_PAGES = 40
PAGE_SIZE = 100


class Command(BaseCommand):
    help = (
        "List Stripe transfer IDs with no matching Payout.stripe_transfer_id (read-only). "
        "Use for reversal decisions in Stripe Dashboard / API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=120,
            help="Only include Stripe transfers created within this many days (default: 120).",
        )
        parser.add_argument(
            "--destination",
            action="append",
            default=[],
            metavar="ACCT_ID",
            help="Only list transfers to this Connect account id (repeatable).",
        )
        parser.add_argument(
            "--description-contains",
            type=str,
            default="ClassEasily Payout",
            help="Only transfers whose description contains this string (default: ClassEasily Payout). "
            "Use empty string --description-contains '' to disable.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print orphan transfer ids as JSON (for scripts).",
        )

    def handle(self, *args, **options):
        days = max(1, options["days"])
        dest_filters = [d.strip() for d in (options["destination"] or []) if d.strip()]
        desc_filter = (options["description_contains"] or "").strip()
        as_json = options["json"]

        key = getattr(settings, "STRIPE_SECRET_KEY", "") or ""
        key_kind = "LIVE" if key.startswith("sk_live") else "TEST" if key.startswith("sk_test") else "unknown"

        cutoff = datetime.now(tz=py_timezone.utc) - timedelta(days=days)
        cutoff_ts = int(cutoff.timestamp())

        # Every transfer id we have ever recorded (real Stripe ids only)
        db_transfer_ids = set(
            Payout.objects.exclude(stripe_transfer_id__startswith="temp_")
            .exclude(stripe_transfer_id="")
            .values_list("stripe_transfer_id", flat=True)
        )

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
                    # Stripe returns newest first; older pages may still have mixed — stop when whole page old
                    pass

                tid = getattr(tr, "id", "") or ""
                if not tid.startswith("tr_"):
                    continue

                if created < cutoff_ts:
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
                    dollars = str(Decimal(int(amt_cents or 0)) / Decimal(100))
                except Exception:
                    dollars = str(amt_cents)

                meta = stripe_metadata_to_dict(getattr(tr, "metadata", None))

                row = {
                    "transfer_id": tid,
                    "amount": dollars,
                    "currency": cur,
                    "destination": dest,
                    "created": datetime.fromtimestamp(created, tz=py_timezone.utc).isoformat(),
                    "description": desc,
                    "metadata": meta,
                }
                merge_reversal_into_orphan(row, tr)
                orphans.append(row)

            if not getattr(page, "has_more", False):
                break
            # Newest-first: once the oldest row on this page is before the cutoff, stop paging.
            last_created = int(getattr(data[-1], "created", 0) or 0)
            if last_created < cutoff_ts:
                break
            starting_after = data[-1].id

        # Duplicate clusters: same destination + amount + currency (all orphans)
        cluster = defaultdict(list)
        for o in orphans:
            k = (o["destination"], o["amount"], o["currency"])
            cluster[k].append(o)

        if as_json:
            ids = [
                o["transfer_id"]
                for o in orphans
                if o.get("reversal_label") != "fully_reversed"
            ]
            self.stdout.write(json.dumps(ids, indent=2))
            return

        self.stdout.write("=" * 80)
        self.stdout.write("SUGGEST TRANSFER REVERSALS (read-only — no API writes)")
        self.stdout.write(f"Stripe key mode: {key_kind}")
        self.stdout.write(f"Window: transfers created in last {days} days (newest-first scan, max {MAX_PAGES} pages)")
        if dest_filters:
            self.stdout.write(f"Destination filter: {dest_filters}")
        if desc_filter:
            self.stdout.write(f"Description contains: {desc_filter!r}")
        self.stdout.write(f"Known Payout.stripe_transfer_id rows in DB: {len(db_transfer_ids)}")
        self.stdout.write(
            "Each line includes Stripe amount_reversed (fully_reversed = already clawed back)."
        )
        self.stdout.write("=" * 80)

        self.stdout.write(
            self.style.WARNING(
                "\nThese Stripe transfer IDs have NO matching row in your Payout table.\n"
                "They are typical post-crash orphans — confirm in Dashboard before reversing.\n"
                "Reversals require available balance on the connected account.\n"
            )
        )

        if not orphans:
            self.stdout.write(self.style.SUCCESS("\nNo orphan transfers found in this window.\n"))
            return

        self.stdout.write(f"\n--- ORPHAN TRANSFER IDS ({len(orphans)}) — reverse only after manual check ---\n")
        for o in sorted(orphans, key=lambda x: x["created"]):
            rev = o.get("reversal_label", "?")
            r_amt = o.get("amount_reversed_dollars", "?")
            rem = o.get("remaining_dollars", "?")
            self.stdout.write(
                f"  {o['transfer_id']}\t{o['amount']} {o['currency']}\tdest={o['destination']}\tcreated={o['created']}"
            )
            self.stdout.write(
                f"      reversal={rev}\treversed_amt={r_amt}\tremaining_with_host={rem} {o['currency']}"
            )
            if rev == "fully_reversed":
                self.stdout.write("      (no Stripe reversal needed — already fully reversed)")
            if o["metadata"]:
                self.stdout.write(f"      metadata: {json.dumps(o['metadata'], default=str)}")

        dup_clusters = {k: v for k, v in cluster.items() if len(v) > 1}
        if dup_clusters:
            self.stdout.write(
                "\n--- DUPLICATE ORPHAN CLUSTERS (same destination + amount) — often keep one, reverse extras ---\n"
            )
            for (dest, amt, cur), items in sorted(
                dup_clusters.items(), key=lambda x: f"{x[0][0]}|{x[0][1]}|{x[0][2]}"
            ):
                active_n = len([x for x in items if x.get("reversal_label") != "fully_reversed"])
                self.stdout.write(
                    f"\n  {len(items)} x {amt} {cur} -> {dest} ({active_n} still sending funds)"
                )
                for o in sorted(items, key=lambda x: x["created"]):
                    rl = o.get("reversal_label", "?")
                    self.stdout.write(f"    {o['transfer_id']}  ({o['created']})  [{rl}]")

        self.stdout.write("\n" + "=" * 80)
        self.stdout.write("Copy-paste transfer ids (excluding fully_reversed in Stripe):")
        self.stdout.write(
            " ".join(
                o["transfer_id"]
                for o in sorted(orphans, key=lambda x: x["created"])
                if o.get("reversal_label") != "fully_reversed"
            )
        )
        self.stdout.write("\n" + "=" * 80)
