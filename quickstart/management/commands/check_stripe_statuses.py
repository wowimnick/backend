# quickstart/management/commands/check_stripe_statuses.py

import stripe
from django.core.management.base import BaseCommand
from django.conf import settings

# Make sure to import your BusinessInfo model from the correct path
from quickstart.models import BusinessInfo

stripe.api_key = settings.STRIPE_SECRET_KEY


def get_live_stripe_status(stripe_account_obj):
    """
    Calculates the platform status based on a live Stripe Account object.
    This logic is copied directly from the GET method in StripeConnectView
    to ensure consistency.
    """
    # Use getattr for safe access, compatible with Stripe objects
    requirements = getattr(stripe_account_obj, "requirements", {})
    disabled_reason = getattr(stripe_account_obj, "disabled_reason", None)
    currently_due = getattr(requirements, "currently_due", [])
    eventually_due = getattr(requirements, "eventually_due", [])
    pending_verification = getattr(requirements, "pending_verification", [])

    if (
        stripe_account_obj.charges_enabled
        and stripe_account_obj.payouts_enabled
        and not currently_due
        and not eventually_due
        and disabled_reason is None
    ):
        return "active"
    elif disabled_reason is not None or currently_due:
        return "restricted"
    elif pending_verification:
        return "pending"
    elif not stripe_account_obj.details_submitted:
        return "incomplete"
    else:
        return "pending"


class Command(BaseCommand):
    help = "Checks the status of Stripe Connect accounts and reports discrepancies without modifying the database."

    def handle(self, *args, **options):
        self.stdout.write(
            "Starting Stripe status check for potentially out-of-sync accounts..."
        )
        self.stdout.write(
            "This is a READ-ONLY operation and will not make any database changes."
        )
        self.stdout.write("-" * 50)

        # We only need to check accounts that are not already marked as 'active' in our DB.
        businesses_to_check = BusinessInfo.objects.filter(
            stripe_account_id__isnull=False
        ).exclude(stripe_account_status="active")

        if not businesses_to_check.exists():
            self.stdout.write(
                self.style.SUCCESS(
                    "No potentially out-of-sync accounts found to check."
                )
            )
            return

        discrepancy_count = 0

        for business in businesses_to_check:
            self.stdout.write(
                f"\nChecking Business ID: {business.businessId} (Stripe Acc: {business.stripe_account_id})"
            )

            try:
                # Fetch the live account object from Stripe
                live_stripe_account = stripe.Account.retrieve(
                    business.stripe_account_id
                )

                # Determine what the status *should* be based on live data
                live_status = get_live_stripe_status(live_stripe_account)

                # Get the status currently stored in our database
                db_status = business.stripe_account_status

                if live_status != db_status:
                    discrepancy_count += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [MISMATCH FOUND] "
                            f"DB Status: '{db_status}', "
                            f"Live Stripe Status: '{live_status}'"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [OK] DB status ('{db_status}') matches live Stripe status."
                        )
                    )

            except stripe.error.InvalidRequestError as e:
                # This can happen if the account was deleted in Stripe but not in our DB
                if "No such account" in str(e):
                    self.stdout.write(
                        self.style.ERROR(
                            f"  [ERROR] Stripe account '{business.stripe_account_id}' not found in Stripe."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  [ERROR] Stripe API error for account {business.stripe_account_id}: {e}"
                        )
                    )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [ERROR] An unexpected error occurred for account {business.stripe_account_id}: {e}"
                    )
                )

        self.stdout.write("-" * 50)
        self.stdout.write(f"Check complete. Found {discrepancy_count} discrepancies.")
