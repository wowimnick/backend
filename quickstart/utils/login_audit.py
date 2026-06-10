"""Audit logging for successful user logins."""

import logging

from quickstart.models import AuditLog
from quickstart.utils.request_utils import get_client_ip

logger = logging.getLogger(__name__)


def log_user_login(user, request) -> None:
    """Record a successful login in AuditLog with client IP and user agent."""
    try:
        AuditLog.objects.create(
            user=user,
            user_email=user.email,
            action="login",
            details=f"User '{user.email}' logged in successfully.",
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        logger.info("Successful login audited for user: %s", user.email)
    except Exception as audit_error:
        logger.error(
            "Failed to create login audit log for user %s: %s",
            user.email,
            audit_error,
        )
