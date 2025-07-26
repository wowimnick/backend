# quickstart/stripe_webhooks.py

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
import stripe
import logging

from quickstart.models import BusinessInfo, Payout  # Adjust import path as needed

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY
# Use a distinct setting for the Connect webhook secret for security
STRIPE_CONNECT_WEBHOOK_SECRET = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)


def _update_business_status_from_stripe_account(stripe_account_obj):
    """
    Helper function to determine and update the business status from a Stripe Account object.
    This ensures the logic is consistent everywhere.
    """
    stripe_account_id = stripe_account_obj.id
    logger.info(
        f"--- Helper: Evaluating status for Stripe Account: {stripe_account_id} ---"
    )
    try:
        business = BusinessInfo.objects.get(stripe_account_id=stripe_account_id)
        previous_status = business.stripe_account_status
        logger.info(f"Helper: Current DB status is '{previous_status}'.")

        # --- DETAILED LOGGING OF INCOMING STRIPE DATA ---
        requirements = stripe_account_obj.get("requirements", {})
        disabled_reason = stripe_account_obj.get("disabled_reason")

        currently_due = requirements.get("currently_due", [])
        eventually_due = requirements.get("eventually_due", [])
        pending_verification = requirements.get("pending_verification", [])

        logger.info(
            f"Helper: Stripe Data | charges_enabled: {stripe_account_obj.charges_enabled}"
        )
        logger.info(
            f"Helper: Stripe Data | payouts_enabled: {stripe_account_obj.payouts_enabled}"
        )
        logger.info(
            f"Helper: Stripe Data | details_submitted: {stripe_account_obj.details_submitted}"
        )
        logger.info(f"Helper: Stripe Data | disabled_reason: '{disabled_reason}'")
        logger.info(f"Helper: Stripe Data | currently_due: {currently_due}")
        logger.info(
            f"Helper: Stripe Data | pending_verification: {pending_verification}"
        )
        logger.info(f"Helper: Stripe Data | eventually_due: {eventually_due}")
        # --- END OF DETAILED LOGGING ---

        new_platform_status = "unlinked"  # Default

        if (
            stripe_account_obj.charges_enabled
            and stripe_account_obj.payouts_enabled
            and not currently_due
            and not eventually_due
            and not disabled_reason
        ):
            new_platform_status = "active"
        elif disabled_reason or currently_due:
            # PRIORITY 1: If user action is required, status is ALWAYS restricted.
            new_platform_status = "restricted"
        elif pending_verification:
            # PRIORITY 2: If no action is required but Stripe is reviewing, status is pending.
            new_platform_status = "pending"
        elif not stripe_account_obj.details_submitted:
            # If the user hasn't submitted their initial details yet.
            new_platform_status = "incomplete"
        else:
            # A general fallback for other states (like only eventually_due items).
            new_platform_status = "pending"

        logger.info(f"Helper: Final determined status is '{new_platform_status}'.")

        if previous_status != new_platform_status:
            business.stripe_account_status = new_platform_status
            business.save(update_fields=["stripe_account_status"])
            logger.info(
                f"Helper: SUCCESS! Business {business.businessId} status updated from '{previous_status}' to '{new_platform_status}'."
            )
        else:
            logger.info(
                f"Helper: Status '{new_platform_status}' is unchanged. No DB update needed."
            )

        return True

    except BusinessInfo.DoesNotExist:
        logger.error(
            f"Webhook (Helper): Received event for unknown Stripe account ID: {stripe_account_id}"
        )
        return False  # Indicates business not found
    except Exception as e:
        logger.error(
            f"Webhook (Helper): Error processing account {stripe_account_id}: {e}",
            exc_info=True,
        )
        # Re-raise the exception to be handled by the main function
        raise


@csrf_exempt
def stripe_connect_webhook(request):
    if request.method != "POST":
        logger.warning("Webhook: Received non-POST request.")
        return HttpResponse("Request method not allowed.", status=405)

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    event = None

    if not STRIPE_CONNECT_WEBHOOK_SECRET:
        logger.error(
            "Stripe Connect webhook secret (STRIPE_CONNECT_WEBHOOK_SECRET) not configured."
        )
        return HttpResponse("Webhook secret not configured.", status=500)

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_CONNECT_WEBHOOK_SECRET
        )
        logger.info(
            f"--- Connect Webhook: Received event ID: {event.id}, Type: {event.type} ---"
        )
    except ValueError as e:
        logger.error(f"Connect Webhook: Invalid payload: {e}")
        return HttpResponse("Invalid payload", status=400)
    except stripe.SignatureVerificationError as e:
        logger.error(f"Connect Webhook: Invalid signature: {e}")
        return HttpResponse("Invalid signature", status=400)
    except Exception as e:
        logger.error(f"Connect Webhook: Error constructing event: {e}", exc_info=True)
        return HttpResponse("Webhook error during construction", status=500)

    # Handle the event
    if event.type == "account.updated":
        account = event.data.object
        logger.info(f"Connect Webhook: Processing 'account.updated' for {account.id}")
        try:
            _update_business_status_from_stripe_account(account)
        except Exception:
            # Errors are logged in the helper, return 500 to signal processing failure to Stripe
            return HttpResponse(
                "Webhook error during account.updated processing", status=500
            )

    elif event.type == "capability.updated":
        capability = event.data.object
        stripe_account_id = capability.account
        logger.info(
            f"Connect Webhook: Processing 'capability.updated' for {stripe_account_id}. Capability: {capability.id}, Status: {capability.status}"
        )
        try:
            # When a capability updates, re-fetch the entire account to get the complete picture
            account = stripe.Account.retrieve(stripe_account_id)
            _update_business_status_from_stripe_account(account)
        except stripe.StripeError as e:
            logger.error(
                f"Connect Webhook: Stripe error retrieving account {stripe_account_id} on capability update: {e}"
            )
            return HttpResponse(
                "Stripe error during capability.updated processing", status=500
            )
        except Exception:
            # Errors are logged in the helper, return 500 to signal processing failure to Stripe
            return HttpResponse(
                "Webhook error during capability.updated processing", status=500
            )

    elif event.type in [
        "transfer.created",
        "transfer.paid",
        "transfer.failed",
        "transfer.updated",
    ]:
        transfer = event.data.object
        stripe_transfer_id = transfer.id
        new_status = transfer.status  # e.g., 'paid', 'pending', 'failed'
        logger.info(
            f"Connect Webhook: Processing '{event.type}' for transfer {stripe_transfer_id} with new status '{new_status}'"
        )
        try:
            # Update the local Payout record based on the Stripe transfer status
            payout_record, updated = Payout.objects.update_or_create(
                stripe_transfer_id=stripe_transfer_id, defaults={"status": new_status}
            )
            if updated:
                logger.info(
                    f"Payout {payout_record.id} status updated to '{new_status}'."
                )

            # If a transfer fails, you might want to revert the bookings
            if new_status == "failed":
                logger.warning(
                    f"Transfer {stripe_transfer_id} failed. Reverting associated bookings' payout_status to 'pending'."
                )
                payout_record.bookings.all().update(payout_status="pending")
                # TODO: Trigger an admin notification for the failed transfer

        except Exception as e:
            logger.error(
                f"Error updating payout status for transfer {stripe_transfer_id}: {e}",
                exc_info=True,
            )
            return HttpResponse("Webhook error during transfer processing", status=500)

    else:
        logger.info(
            f"Connect Webhook: Unhandled event type {event.type} (ID: {event.id})"
        )

    # Acknowledge receipt of the event to Stripe
    return HttpResponse(status=200)

    # Acknowledge receipt of the event to Stripe
    return HttpResponse(status=200)
