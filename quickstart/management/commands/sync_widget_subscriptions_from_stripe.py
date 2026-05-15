"""
Re-sync widget, addon, and customer membership rows from Stripe (repair missed webhooks).
"""
import logging

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from quickstart.models import WidgetSubscription, BusinessAddonSubscription, CustomerMembership
from quickstart.services.subscription_sync import (
    sync_widget_subscription_from_stripe,
    sync_addon_subscription_from_stripe,
)
from quickstart.models import ADDON_TYPE_EMAIL_MARKETING, ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING
from quickstart.services.membership_sync import sync_customer_membership_from_stripe

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync WidgetSubscription / BusinessAddonSubscription / CustomerMembership from Stripe"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=500,
            help="Max rows per model to process",
        )

    def handle(self, *args, **options):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        limit = options["limit"]

        ws = list(
            WidgetSubscription.objects.exclude(
                stripe_subscription_id__isnull=True
            ).exclude(stripe_subscription_id="")[:limit]
        )
        self.stdout.write(f"Syncing {len(ws)} widget subscription(s)…")
        for row in ws:
            sub, err = sync_widget_subscription_from_stripe(row.stripe_subscription_id)
            if err:
                self.stdout.write(
                    self.style.WARNING(f"  Widget sub {row.stripe_subscription_id}: {err}")
                )
            elif sub:
                self.stdout.write(f"  OK widget business={sub.business_id}")

        addons = list(
            BusinessAddonSubscription.objects.exclude(
                stripe_subscription_id__isnull=True
            ).exclude(stripe_subscription_id="")[:limit]
        )
        self.stdout.write(f"Syncing {len(addons)} addon subscription(s)…")
        for row in addons:
            at = row.addon_type
            if at not in (
                ADDON_TYPE_MARKETPLACE_EMAIL_BRANDING,
                ADDON_TYPE_EMAIL_MARKETING,
            ):
                continue
            try:
                sync_addon_subscription_from_stripe(
                    row.stripe_subscription_id, addon_type=at
                )
                self.stdout.write(f"  OK addon {row.stripe_subscription_id} ({at})")
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Addon {row.stripe_subscription_id}: {e}"
                    )
                )

        cms = list(
            CustomerMembership.objects.exclude(
                stripe_subscription_id__isnull=True
            ).exclude(stripe_subscription_id="")[:limit]
        )
        self.stdout.write(f"Syncing {len(cms)} customer membership(s)…")
        for row in cms:
            m, err = sync_customer_membership_from_stripe(row.stripe_subscription_id)
            if err:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Membership {row.stripe_subscription_id}: {err}"
                    )
                )
            elif m:
                self.stdout.write(f"  OK membership {m.id}")

        self.stdout.write(self.style.SUCCESS("Stripe sync pass complete."))
