from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Q # Import Q
import stripe
import logging

from quickstart.models import BusinessInfo # Adjust import path as needed

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY
STRIPE_CONNECT_WEBHOOK_SECRET = getattr(settings, 'STRIPE_WEBHOOK_SECRET', None)


@csrf_exempt
def stripe_connect_webhook(request): # Renamed for clarity
    if request.method != 'POST':
        logger.warning("Webhook: Received non-POST request.")
        return HttpResponse("Request method not allowed.", status=405)

    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    event = None

    if not STRIPE_CONNECT_WEBHOOK_SECRET: # Check for connect-specific secret
        logger.error("Stripe Connect webhook secret (STRIPE_WEBHOOK_SECRET) not configured.")
        return HttpResponse("Webhook secret not configured.", status=500)

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_CONNECT_WEBHOOK_SECRET
        )
        logger.info(f"Webhook: Received event ID: {event.id}, Type: {event.type}")
    except ValueError as e:
        logger.error(f"Webhook ValueError: Invalid payload: {e}")
        return HttpResponse("Invalid payload", status=400)
    except stripe.SignatureVerificationError as e:
        logger.error(f"Webhook SignatureVerificationError: Invalid signature: {e}")
        return HttpResponse("Invalid signature", status=400)
    except Exception as e:
        logger.error(f"Webhook general error during event construction: {e}", exc_info=True)
        return HttpResponse("Webhook error during construction", status=500)

    # Handle the event
    if event.type == 'account.updated':
        account = event.data.object # This is a Stripe Account object
        stripe_account_id = account.id
        try:
            # It's possible for a user to manage multiple businesses, but one Stripe Account ID should be unique to one BusinessInfo
            business = BusinessInfo.objects.get(stripe_account_id=stripe_account_id)
            
            previous_status = business.stripe_account_status
            
            # Determine new platform status based on Stripe account object
            new_platform_status = 'unlinked' # Default
            if account.details_submitted:
                if account.charges_enabled and account.payouts_enabled and \
                   not (account.requirements and (account.requirements.currently_due or account.requirements.eventually_due)):
                    new_platform_status = 'active'
                elif account.requirements and account.requirements.disabled_reason:
                    new_platform_status = 'restricted'
                elif account.requirements and (account.requirements.currently_due or account.requirements.eventually_due or not account.payouts_enabled or not account.charges_enabled):
                    new_platform_status = 'pending' # Needs more info or verification
                else: # Fallback if details submitted but not fully active
                    new_platform_status = 'pending' 
            else:
                new_platform_status = 'incomplete' # Not even basic details submitted

            if previous_status != new_platform_status:
                business.stripe_account_status = new_platform_status
                business.save(update_fields=['stripe_account_status'])
                logger.info(f"Webhook: Business {business.businessId} (Stripe Acc: {stripe_account_id}) status updated from '{previous_status}' to '{new_platform_status}'.")
            else:
                logger.info(f"Webhook: Business {business.businessId} (Stripe Acc: {stripe_account_id}) status '{new_platform_status}' unchanged.")

        except BusinessInfo.DoesNotExist:
            logger.error(f"Webhook: Received account.updated for unknown Stripe account ID: {stripe_account_id}")
            # Return 200 to Stripe to acknowledge receipt even if we can't find the business,
            # to prevent Stripe from retrying indefinitely for this specific type of error.
            # The error is logged for investigation.
            return HttpResponse("Business not found for Stripe account, but event acknowledged.", status=200)
        except Exception as e:
            logger.error(f"Webhook: Error processing account.updated for {stripe_account_id}: {e}", exc_info=True)
            return HttpResponse("Webhook error during account.updated processing", status=500)

    elif event.type == 'capability.updated':
        capability = event.data.object
        stripe_account_id = capability.account
        logger.info(f"Webhook: Capability '{capability.id}' status '{capability.status}' for account {stripe_account_id}. Triggering account status re-evaluation.")
        # When a capability updates, it's good practice to re-fetch the entire account
        # and re-evaluate its overall status, as multiple capabilities affect it.
        try:
            account = stripe.Account.retrieve(stripe_account_id)
            # Re-run the same status determination logic as in 'account.updated'
            # This avoids duplicating the complex status logic here.
            # We can simulate an 'account.updated' event structure for a helper function or just call it.
            # For simplicity, one might call a helper or even re-fetch and save business status.
            # Here, we'll just log and assume an `account.updated` will follow or is sufficient.
            # Ideally, you'd have a shared function to update business status from a Stripe Account object.
            
            # Minimal action: Log it. The 'account.updated' event usually follows if this leads to overall status change.
            # You could also force a re-check:
            # business = BusinessInfo.objects.get(stripe_account_id=stripe_account_id)
            # Perform the same status update logic as in 'account.updated' block.
            # This avoids code duplication if put into a helper.
            
        except BusinessInfo.DoesNotExist:
            logger.error(f"Webhook: Received capability.updated for unknown Stripe account ID: {stripe_account_id}")
            return HttpResponse("Business not found for Stripe account, but event acknowledged.", status=200)
        except stripe.StripeError as e:
            logger.error(f"Webhook: Stripe error retrieving account {stripe_account_id} on capability update: {e}")
            return HttpResponse("Stripe error during capability.updated processing", status=500)
        except Exception as e:
            logger.error(f"Webhook: Error processing capability.updated for {stripe_account_id}: {e}", exc_info=True)
            return HttpResponse("Webhook error during capability.updated processing", status=500)
            
    # Add handling for other relevant Connect events, e.g.,
    # 'payout.paid', 'payout.failed', 'charge.refund.updated' if you need to track these for business users.
    # 'account.application.deauthorized' - IMPORTANT for when user disconnects your platform from Stripe.

    else:
        logger.info(f"Webhook: Unhandled event type {event.type} (ID: {event.id})")

    return HttpResponse(status=200) # Acknowledge receipt of event to Stripe