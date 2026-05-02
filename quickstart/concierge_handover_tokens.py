"""Token generator for concierge business handover (claim-account) links."""

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int


class ConciergeHandoverTokenGenerator(PasswordResetTokenGenerator):
    """
    Same construction as Django's PasswordResetTokenGenerator, but the link is not
    invalidated by PASSWORD_RESET_TIMEOUT. It still becomes invalid after the user
    sets a new password (password hash is part of the signed material).
    """

    key_salt = "quickstart.concierge_handover.ConciergeHandoverTokenGenerator"

    def check_token(self, user, token):
        if not (user and token):
            return False
        try:
            ts_b36, _ = token.split("-")
        except ValueError:
            return False
        try:
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False
        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(
                self._make_token_with_timestamp(user, ts, secret),
                token,
            ):
                return True
        return False


concierge_handover_token_generator = ConciergeHandoverTokenGenerator()
