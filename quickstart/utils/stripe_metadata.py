"""
Stripe API objects expose ``metadata`` as a StripeObject (dict subclass).
``dict(metadata)``, ``dict.update(metadata)``, and some merges can raise
(e.g. KeyError) — use this helper before JSONField storage or dict methods.
"""


def stripe_metadata_to_dict(metadata):
    """Return a plain dict copy of Stripe ``metadata`` (or empty dict)."""
    if metadata is None:
        return {}
    to_dict = getattr(metadata, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            if isinstance(d, dict):
                return dict(d)
        except Exception:
            pass
    if isinstance(metadata, dict):
        try:
            return dict(metadata.items())
        except Exception:
            return {}
    get_items = getattr(metadata, "items", None)
    if callable(get_items):
        try:
            return dict(get_items())
        except Exception:
            pass
    return {}
