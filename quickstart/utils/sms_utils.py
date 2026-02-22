# quickstart/utils/sms_utils.py
"""
AWS SNS SMS utilities for transactional and campaign SMS.
Uses direct SNS Publish to phone numbers (no topics).
"""
import re
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

# SNS SMS message length limit (chars)
SNS_SMS_MAX_LENGTH = 1600


def normalize_phone_for_sns(phone_number: str) -> str | None:
    """
    Normalize phone to E.164 for SNS.
    Strips non-digits, adds + and default country code +1 if missing.
    Returns None if invalid or empty.
    """
    if not phone_number or not isinstance(phone_number, str):
        return None
    digits = re.sub(r"\D", "", phone_number.strip())
    if not digits:
        return None
    if len(digits) < 10:
        return None
    if digits.startswith("1") and len(digits) == 11:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if not digits.startswith("1"):
        return "+1" + digits
    return "+" + digits


def send_sms(phone_number: str, message: str) -> bool:
    """
    Send one SMS via AWS SNS. Uses default credential chain (instance/task role).
    Returns True on success, False if disabled or on failure.
    """
    if not getattr(settings, "AWS_SMS_ENABLED", False):
        return False
    normalized = normalize_phone_for_sns(phone_number)
    if not normalized:
        logger.warning("send_sms: invalid or empty phone number")
        return False
    if not message or not message.strip():
        logger.warning("send_sms: empty message")
        return False
    if len(message) > SNS_SMS_MAX_LENGTH:
        message = message[: SNS_SMS_MAX_LENGTH - 3].rstrip() + "..."
    try:
        import boto3

        client = boto3.client("sns", region_name=settings.AWS_SNS_REGION)
        client.publish(PhoneNumber=normalized, Message=message)
        logger.info("SMS sent to %s", normalized)
        return True
    except Exception as e:
        logger.exception("SMS send failed to %s: %s", normalized, e)
        return False
