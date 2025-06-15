from django.core.signing import Signer, BadSignature
from django.conf import settings

# Use a specific salt for unsubscribe tokens for better security
UNSUBSCRIBE_SIGNER = Signer(salt='classeasily.notifications.unsubscribe')

def generate_unsubscribe_token(user_id):
    """Generates a signed token for a user ID."""
    return UNSUBSCRIBE_SIGNER.sign(str(user_id))

def verify_unsubscribe_token(token):
    """
    Verifies a signed token and returns the user ID.
    Returns None if the token is invalid.
    """
    try:
        user_id = UNSUBSCRIBE_SIGNER.unsign(token, max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'])
        return int(user_id)
    except (BadSignature, ValueError):
        return None