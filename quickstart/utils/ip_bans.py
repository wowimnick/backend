"""Helpers for banning client IP addresses."""

from __future__ import annotations

from quickstart.models import BannedIP


def ban_ip_addresses(user, ips, reason: str) -> dict[str, list[str]]:
    """
    Ban or reactivate IP addresses.

    Returns lists of IPs that were newly created, reactivated, or already active.
    """
    created: list[str] = []
    reactivated: list[str] = []
    already_active: list[str] = []

    for ip in ips:
        ban, was_created = BannedIP.objects.get_or_create(
            ip_address=ip,
            defaults={
                "reason": reason,
                "created_by": user,
                "is_active": True,
            },
        )
        if was_created:
            created.append(ip)
        elif not ban.is_active:
            ban.is_active = True
            ban.reason = reason
            ban.created_by = user
            ban.save(update_fields=["is_active", "reason", "created_by"])
            reactivated.append(ip)
        else:
            already_active.append(ip)

    return {
        "created": created,
        "reactivated": reactivated,
        "already_active": already_active,
    }
