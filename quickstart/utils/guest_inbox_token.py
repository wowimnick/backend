"""
Signed token for guest (no-account) conversation inbox.
Token payload: (conversation_id, contact_id). Expiry embedded in signed value.
"""

import logging
from django.core.signing import Signer, BadSignature, SignatureExpired
from django.conf import settings

logger = logging.getLogger(__name__)

SALT = "guest_inbox"
# Default 30 days; can override with GUEST_INBOX_TOKEN_MAX_AGE_SECONDS
DEFAULT_MAX_AGE_DAYS = 30


def get_max_age_seconds():
    import os
    return int(os.environ.get("GUEST_INBOX_TOKEN_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_DAYS * 24 * 3600))


def create_guest_inbox_token(conversation_id, contact_id):
    """Create a signed token for guest inbox. Token is valid for GUEST_INBOX_TOKEN_MAX_AGE."""
    from django.core.signing import dumps
    key = f"{conversation_id}:{contact_id}"
    return dumps(key, salt=SALT)


def parse_guest_inbox_token(token):
    """
    Parse and validate token. Returns (conversation_id, contact_id) or None if invalid/expired.
    """
    from django.core.signing import loads, SignatureExpired, BadSignature
    try:
        key = loads(token, salt=SALT, max_age=get_max_age_seconds())
        parts = key.split(":", 1)
        if len(parts) != 2:
            return None
        return (parts[0], parts[1])
    except (BadSignature, SignatureExpired, ValueError) as e:
        logger.debug("Invalid guest inbox token: %s", e)
        return None
