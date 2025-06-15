# quickstart/webhooks/views.py

import json
import logging
import time
import hmac
import hashlib
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db.models import F
from django.conf import settings
from ...models import NotificationCampaign

logger = logging.getLogger(__name__)

# --- Helper function for webhook verification ---
def verify_resend_webhook(request):
    """
    Manually verifies the Resend webhook signature.
    Returns the parsed event payload if valid, otherwise raises an exception.
    """
    signature_header = request.headers.get('Resend-Signature')
    if not signature_header:
        raise ValueError("Signature header missing")

    # 1. Parse the header
    try:
        timestamp_str, signature = [item.split('=')[1] for item in signature_header.split(',')]
        timestamp = int(timestamp_str)
    except (ValueError, IndexError):
        raise ValueError("Invalid signature header format")

    # 2. Check for replay attacks (optional but recommended)
    # Reject webhooks older than 5 minutes
    if time.time() - timestamp > 300:
        raise ValueError("Webhook timestamp is too old")

    # 3. Reconstruct the signed payload
    raw_body = request.body
    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode('utf-8')
    
    # 4. Compute the expected signature
    secret = settings.RESEND_WEBHOOK_SECRET.encode('utf-8')
    expected_signature = hmac.new(
        key=secret,
        msg=signed_payload,
        digestmod=hashlib.sha256
    ).hexdigest()

    # 5. Compare signatures securely
    if not hmac.compare_digest(expected_signature, signature):
        raise ValueError("Signature mismatch")
        
    # If we get here, the signature is valid. Return the parsed JSON body.
    return json.loads(raw_body)


@csrf_exempt
@require_POST
def resend_webhook_receiver(request):
    """
    Receives and processes webhooks from Resend for email events.
    Verifies the request signature using manual HMAC-SHA256 comparison.
    """
    try:
        # Use the helper to verify and get the payload
        payload = verify_resend_webhook(request)
        
        event_type = payload.get('type')
        data = payload.get('data', {})
        
        logger.info(f"Resend webhook verified. Type: {event_type}")

        # Extract campaign_id from headers
        headers = {h['name']: h['value'] for h in data.get('headers', [])}
        campaign_id = headers.get('X-Campaign-ID')

        if not campaign_id:
            logger.warning(f"Webhook event '{event_type}' received without a Campaign ID. Skipping.")
            return JsonResponse({'status': 'ok', 'message': 'Event ignored, no campaign ID.'})

        # Process the event based on its type
        if event_type == 'email.delivered':
            # Use atomic update to prevent race conditions
            NotificationCampaign.objects.filter(id=campaign_id).update(
                delivered_count=F('delivered_count') + 1
            )
            logger.info(f"Processed 'delivered' event for campaign {campaign_id}.")

        elif event_type == 'email.bounced':
            logger.warning(f"Processing 'bounced' event for campaign {campaign_id}.")
            # Future: Update a bounce counter or log the bounce reason
            
        elif event_type == 'email.clicked':
            # This counts every click. For unique clicks, a more complex system
            # involving caching user IDs would be needed. This is a good start.
            NotificationCampaign.objects.filter(id=campaign_id).update(
                click_count=F('click_count') + 1
            )
            logger.info(f"Processed 'clicked' event for campaign {campaign_id}.")

        else:
            logger.info(f"Ignoring unhandled event type: {event_type}")

        return JsonResponse({'status': 'ok'})

    except ValueError as e:
        # This will catch signature verification errors
        logger.error(f"Webhook verification failed: {str(e)}")
        return JsonResponse({'error': f'Webhook Error: {str(e)}'}, status=400)
    except Exception as e:
        # Catch any other processing errors
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return JsonResponse({'error': 'Internal Server Error'}, status=500)