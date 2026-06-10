"""Shared HTTP request helpers."""


def get_client_ip(request):
    """Get client IP from X-Forwarded-For (first hop) or REMOTE_ADDR."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")

