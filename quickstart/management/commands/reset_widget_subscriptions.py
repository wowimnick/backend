"""
Reset all widget subscriptions to none (delete from DB, optional Stripe cancel).
Use for testing the full subscription flow from scratch.

Examples:
  python manage.py reset_widget_subscriptions
  python manage.py reset_widget_subscriptions --cancel-stripe
  python manage.py reset_widget_subscriptions --dry-run
"""
import logging
from django.core.management.base import BaseCommand
from django.conf import settings

from quickstart.models import WidgetSubscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Reset all widget subscriptions so the app behaves as if no one has a plan (for testing)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only print what would be deleted, do not delete.",
        )
        parser.add_argument(
            "--cancel-stripe",
            action="store_true",
            help="Cancel the subscription in Stripe before deleting the DB row (cleaner for testing).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        cancel_stripe = options["cancel_stripe"]

        qs = WidgetSubscription.objects.all()
        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING("No widget subscriptions found. Already reset."))
            return

        if dry_run:
            self.stdout.write(f"Would delete {total} widget subscription(s):")
            for sub in qs.select_related("business"):
                biz = sub.business
                name = getattr(biz, "businessName", None) or getattr(biz, "id", "")
                self.stdout.write(
                    f"  - business={name} plan_id={sub.plan_id} stripe_sub_id={sub.stripe_subscription_id or '(none)'}"
                )
            return

        if cancel_stripe and getattr(settings, "STRIPE_SECRET_KEY", None):
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY
            for sub in qs:
                if sub.stripe_subscription_id:
                    try:
                        stripe.Subscription.cancel(sub.stripe_subscription_id)
                        self.stdout.write(f"Canceled Stripe subscription {sub.stripe_subscription_id}")
                    except stripe.StripeError as e:
                        self.stdout.write(
                            self.style.WARNING(f"Stripe cancel failed for {sub.stripe_subscription_id}: {e}")
                        )
        elif cancel_stripe:
            self.stdout.write(self.style.WARNING("STRIPE_SECRET_KEY not set; skipping Stripe cancel."))

        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} widget subscription(s). You can test from scratch now."))
