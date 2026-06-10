"""Audit logging for successful user logins."""

import logging

from django.db.models import Q

from quickstart.models import AuditLog, ConversationMessage
from quickstart.utils.request_utils import get_client_ip

logger = logging.getLogger(__name__)


def log_user_login(user, request) -> None:
    """Record a successful login in AuditLog with client IP and user agent."""
    client_ip = get_client_ip(request)
    try:
        AuditLog.objects.create(
            user=user,
            user_email=user.email,
            action="login",
            details=f"User '{user.email}' logged in successfully.",
            ip_address=client_ip,
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        logger.info(
            "Successful login audited for user=%s ip=%s path=%s",
            user.email,
            client_ip or "unknown",
            getattr(request, "path", ""),
        )
        if not client_ip:
            logger.warning(
                "Login audit for user=%s saved without IP (check proxy headers)",
                user.email,
            )
    except Exception as audit_error:
        logger.error(
            "Failed to create login audit log for user %s: %s",
            user.email,
            audit_error,
        )


def collect_account_ip_addresses(user) -> list[str]:
    """
    Gather known client IPs for a user account from audit logs and messaging activity.
    """
    ips: set[str] = set()

    login_logs = AuditLog.objects.filter(action="login").filter(
        Q(user=user) | Q(user__isnull=True, user_email__iexact=user.email)
    )
    ips.update(
        login_logs.exclude(ip_address__isnull=True)
        .exclude(ip_address="")
        .values_list("ip_address", flat=True)
    )

    activity_logs = AuditLog.objects.filter(
        Q(user=user) | Q(user_email__iexact=user.email)
    )
    ips.update(
        activity_logs.exclude(ip_address__isnull=True)
        .exclude(ip_address="")
        .values_list("ip_address", flat=True)
    )

    ips.update(
        ConversationMessage.objects.filter(sender_user=user)
        .exclude(sender_ip__isnull=True)
        .exclude(sender_ip="")
        .values_list("sender_ip", flat=True)
    )

    return sorted(ips)
