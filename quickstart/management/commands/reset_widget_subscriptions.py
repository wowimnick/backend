"""
Reset all widget subscriptions, addon subscriptions, and billing state (optional Stripe cancel + void open/draft invoices).
Use for testing the full subscription and billing flow from scratch.

Examples:
  python manage.py reset_widget_subscriptions
  python manage.py reset_widget_subscriptions --cancel-stripe
  python manage.py reset_widget_subscriptions --dry-run
"""
import logging
from django.core.management.base import BaseCommand
from django.conf import settings

from quickstart.models import WidgetSubscription, BusinessAddonSubscription

logger = logging.getLogger(__name__)


def _collect_stripe_customer_ids(widget_subs, addon_subs):
    """Collect unique Stripe customer IDs from subscriptions and their businesses."""
    seen = set()
    for sub in widget_subs.select_related("business"):
        if sub.stripe_customer_id and sub.stripe_customer_id.strip():
            seen.add(sub.stripe_customer_id.strip())
        biz = sub.business
        if biz and getattr(biz, "stripe_customer_id", None) and biz.stripe_customer_id.strip():
            seen.add(biz.stripe_customer_id.strip())
    for sub in addon_subs.select_related("business"):
        if sub.stripe_customer_id and sub.stripe_customer_id.strip():
            seen.add(sub.stripe_customer_id.strip())
        biz = sub.business
        if biz and getattr(biz, "stripe_customer_id", None) and biz.stripe_customer_id.strip():
            seen.add(biz.stripe_customer_id.strip())
    return seen


def _void_open_and_delete_draft_invoices(stripe_api, customer_ids, stdout, style):
    """For each Stripe customer, void open invoices and delete draft invoices."""
    for cid in customer_ids:
        try:
            # Open invoices: void them (clears pending payment from billing UI)
            open_list = stripe_api.Invoice.list(customer=cid, status="open", limit=100)
            for inv in getattr(open_list, "data", None) or []:
                inv_id = getattr(inv, "id", None) or (
                    inv.get("id") if isinstance(inv, dict) else None
                )
                try:
                    stripe_api.Invoice.void_invoice(inv_id)
                    stdout.write(f"  Voided open invoice {inv_id} (customer {cid})")
                except stripe_api.StripeError as e:
                    stdout.write(style.WARNING(f"  Failed to void invoice {inv_id}: {e}"))

            # Draft invoices: delete them
            draft_list = stripe_api.Invoice.list(customer=cid, status="draft", limit=100)
            for inv in getattr(draft_list, "data", None) or []:
                inv_id = getattr(inv, "id", None) or (
                    inv.get("id") if isinstance(inv, dict) else None
                )
                try:
                    stripe_api.Invoice.delete(inv_id)
                    stdout.write(f"  Deleted draft invoice {inv_id} (customer {cid})")
                except stripe_api.StripeError as e:
                    stdout.write(style.WARNING(f"  Failed to delete draft invoice {inv_id}: {e}"))
        except stripe_api.StripeError as e:
            stdout.write(style.WARNING(f"  Invoice list failed for customer {cid}: {e}"))


class Command(BaseCommand):
    help = (
        "Reset widget subscriptions, addon subscriptions, and billing state "
        "(optional Stripe cancel + void open/draft invoices) for testing from scratch."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print what would be deleted, do not delete.",
        )
        parser.add_argument(
            "--cancel-stripe",
            action="store_true",
            help=(
                "Cancel subscriptions in Stripe and void/delete open and draft invoices "
                "before deleting DB rows (clean slate for testing)."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        cancel_stripe = options["cancel_stripe"]

        widget_qs = WidgetSubscription.objects.all()
        addon_qs = BusinessAddonSubscription.objects.all()
        widget_count = widget_qs.count()
        addon_count = addon_qs.count()

        if widget_count == 0 and addon_count == 0:
            self.stdout.write(self.style.WARNING("No widget or addon subscriptions found. Already reset."))
            return

        if dry_run:
            self.stdout.write("Would perform full billing/subscription reset:")
            if widget_count:
                self.stdout.write(f"  Widget subscriptions: {widget_count}")
                for sub in widget_qs.select_related("business"):
                    biz = sub.business
                    name = getattr(biz, "businessName", None) or getattr(biz, "id", "")
                    self.stdout.write(
                        f"    - business={name} plan_id={sub.plan_id} stripe_sub_id={sub.stripe_subscription_id or '(none)'}"
                    )
            if addon_count:
                self.stdout.write(f"  Addon subscriptions: {addon_count}")
                for sub in addon_qs.select_related("business"):
                    biz = sub.business
                    name = getattr(biz, "businessName", None) or getattr(biz, "id", "")
                    self.stdout.write(
                        f"    - business={name} addon_type={sub.addon_type} stripe_sub_id={sub.stripe_subscription_id or '(none)'}"
                    )
            if cancel_stripe:
                customer_ids = _collect_stripe_customer_ids(widget_qs, addon_qs)
                if customer_ids:
                    self.stdout.write(f"  Would void open and delete draft Stripe invoices for {len(customer_ids)} customer(s).")
            return

        stripe_api = None
        if cancel_stripe and getattr(settings, "STRIPE_SECRET_KEY", None):
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe_api = stripe

            # 1) Cancel addon subscriptions in Stripe
            for sub in addon_qs:
                if sub.stripe_subscription_id:
                    try:
                        stripe_api.Subscription.cancel(sub.stripe_subscription_id)
                        self.stdout.write(f"Canceled Stripe addon subscription {sub.stripe_subscription_id}")
                    except stripe_api.StripeError as e:
                        self.stdout.write(
                            self.style.WARNING(f"Stripe addon cancel failed for {sub.stripe_subscription_id}: {e}")
                        )

            # 2) Cancel widget subscriptions in Stripe
            for sub in widget_qs:
                if sub.stripe_subscription_id:
                    try:
                        stripe_api.Subscription.cancel(sub.stripe_subscription_id)
                        self.stdout.write(f"Canceled Stripe widget subscription {sub.stripe_subscription_id}")
                    except stripe_api.StripeError as e:
                        self.stdout.write(
                            self.style.WARNING(f"Stripe widget cancel failed for {sub.stripe_subscription_id}: {e}")
                        )

            # 3) Void open and delete draft invoices for affected customers (clean billing/transactions UI)
            customer_ids = _collect_stripe_customer_ids(widget_qs, addon_qs)
            if customer_ids:
                self.stdout.write("Voiding open and deleting draft invoices for affected customers:")
                _void_open_and_delete_draft_invoices(stripe_api, customer_ids, self.stdout, self.style)

        elif cancel_stripe:
            self.stdout.write(self.style.WARNING("STRIPE_SECRET_KEY not set; skipping Stripe cancel and invoice cleanup."))

        # 4) Delete addon subscriptions from DB
        addon_deleted, _ = addon_qs.delete()
        if addon_deleted:
            self.stdout.write(self.style.SUCCESS(f"Deleted {addon_deleted} addon subscription(s)."))

        # 5) Delete widget subscriptions from DB
        widget_deleted, _ = widget_qs.delete()
        if widget_deleted:
            self.stdout.write(self.style.SUCCESS(f"Deleted {widget_deleted} widget subscription(s)."))

        self.stdout.write(self.style.SUCCESS("Billing and subscriptions reset. You can test from scratch now."))
