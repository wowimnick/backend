"""Singleton Typesense client."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

try:
    from typesense.exceptions import ObjectNotFound, RequestMalformed, ServiceUnavailable
except ImportError:
    class ObjectNotFound(Exception):
        """Fallback when typesense is not installed."""

    class RequestMalformed(Exception):
        """Fallback when typesense is not installed."""

    class ServiceUnavailable(Exception):
        """Fallback when typesense is not installed."""

_client = None
_TYPESENSE_HEALTH_CACHE_TTL = 30
_TYPESENSE_HEALTH_NEGATIVE_CACHE_TTL = 5


def _typesense_health_cache_key() -> str:
    env = str(getattr(settings, "DJANGO_ENV", "local") or "local")
    host = getattr(settings, "TYPESENSE_HOST", "localhost")
    return f"{env}:typesense_health_ok:{host}"


def _ping_typesense_health(client) -> bool:
    """Lightweight health ping; compatible with typesense==0.21.x."""
    ops = getattr(client, "operations", None)
    if ops is not None and callable(getattr(ops, "is_healthy", None)):
        return bool(ops.is_healthy())
    # Client is configured but health API is unknown — do not block search.
    return True


def get_typesense_client():
    global _client
    if _client is not None:
        return _client
    api_key = getattr(settings, "TYPESENSE_API_KEY", "") or ""
    if not api_key.strip():
        return None
    try:
        import typesense
    except ImportError:
        logger.warning("typesense package not installed")
        return None

    host = getattr(settings, "TYPESENSE_HOST", "localhost")
    port = getattr(settings, "TYPESENSE_PORT", "8108")
    protocol = getattr(settings, "TYPESENSE_PROTOCOL", "http")
    _client = typesense.Client(
        {
            "nodes": [{"host": host, "port": port, "protocol": protocol}],
            "api_key": api_key,
            "connection_timeout_seconds": 4,
            "num_retries": 2,
            "retry_interval_seconds": 0.1,
        }
    )
    return _client


def invalidate_typesense_health_cache() -> None:
    """Clears the cached 'healthy' status so the next request re-checks Typesense health.

    Call this whenever a ServiceUnavailable exception is caught so that callers
    don't keep treating Typesense as healthy for the remainder of the cache TTL.
    """
    cache_key = _typesense_health_cache_key()
    cache.delete(cache_key)


def typesense_available() -> bool:
    """True when Typesense is configured and responds to a lightweight health check."""
    if get_typesense_client() is None:
        return False

    cache_key = _typesense_health_cache_key()
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)

    ok = False
    try:
        client = get_typesense_client()
        if client is not None:
            ok = _ping_typesense_health(client)
    except Exception as e:
        logger.warning("Typesense health check failed: %s", e)

    ttl = _TYPESENSE_HEALTH_CACHE_TTL if ok else _TYPESENSE_HEALTH_NEGATIVE_CACHE_TTL
    cache.set(cache_key, ok, ttl)
    return ok
