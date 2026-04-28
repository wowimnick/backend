"""
Distributed rate limiter + 429-aware retry for the Google Gemini API.

Why this exists
---------------
Google slashed the free-tier Gemini quotas in Dec 2025 (Flash dropped to
10 RPM / 250 RPD with hard enforcement). Without throttling, our Celery
workers fan out generate_content calls fast enough to trip RESOURCE_EXHAUSTED
within seconds. This module ensures we never exceed the per-minute budget,
no matter how many workers or web processes are calling concurrently.

How it works
------------
Fixed 60-second windows keyed on floor(unix_time / 60) so every process
agrees on the same window without coordination. The counter lives in Django
cache (Redis in prod, LocMem in dev), incremented atomically. If we exceed
the budget we sleep until the next window opens.

On 429 we additionally honour the `retryDelay` field that Gemini returns
in the error body and retry up to GEMINI_MAX_RETRIES times with jitter.

Tunable settings (optional, with sane defaults):
  GEMINI_MAX_RPM        - max requests per 60s window (default 8)
  GEMINI_MAX_RETRIES    - 429 retry attempts (default 3)
  GEMINI_MAX_BLOCK_WAIT - max seconds to wait for a slot (default 75)
"""
import logging
import random
import re
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


_DEFAULT_MAX_RPM = 8
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_MAX_BLOCK_WAIT = 75
_RPM_WINDOW_SECONDS = 60
_RATE_KEY_PREFIX = "gemini:rpm"
_INITIAL_BACKOFF = 5.0
_MAX_BACKOFF = 30.0


def _setting(name: str, default):
    try:
        return type(default)(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _acquire_slot() -> bool:
    """
    Block until we own a Gemini RPM slot, or the configured timeout elapses.
    Returns True if a slot was acquired, False if we gave up.
    """
    max_rpm = _setting("GEMINI_MAX_RPM", _DEFAULT_MAX_RPM)
    timeout = _setting("GEMINI_MAX_BLOCK_WAIT", _DEFAULT_MAX_BLOCK_WAIT)
    deadline = time.monotonic() + timeout

    while True:
        window = int(time.time()) // _RPM_WINDOW_SECONDS
        key = f"{_RATE_KEY_PREFIX}:{window}"

        # Initialise the counter atomically. TTL > window so it can't expire
        # mid-increment; it'll be discarded naturally once the window passes.
        cache.add(key, 0, timeout=_RPM_WINDOW_SECONDS + 30)
        try:
            count = cache.incr(key)
        except ValueError:
            # Key expired between add() and incr(); loop and re-add.
            continue

        if count <= max_rpm:
            return True

        # Over budget for this window. Sleep until the next one opens.
        now = time.time()
        seconds_into_window = now - (window * _RPM_WINDOW_SECONDS)
        sleep_for = max(0.5, _RPM_WINDOW_SECONDS - seconds_into_window)
        sleep_for += random.uniform(0, 0.5)  # jitter to avoid thundering herd

        if time.monotonic() + sleep_for > deadline:
            logger.warning(
                "Gemini RPM slot not acquired within %.1fs (budget %s/min); giving up.",
                timeout, max_rpm,
            )
            return False

        logger.info(
            "Gemini RPM budget hit (%s/%s in current window); sleeping %.1fs.",
            count, max_rpm, sleep_for,
        )
        time.sleep(sleep_for)


# Matches "retryDelay": "37s" or retryDelay='37.26s' inside the error string.
_RETRY_DELAY_RE = re.compile(
    r"retry[_-]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s",
    re.IGNORECASE,
)


def _is_rate_limited(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "429" in msg or "resource_exhausted" in msg or "resource exhausted" in msg:
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 429


def _retry_seconds(exc: BaseException, fallback: float) -> float:
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return fallback


def gemini_call(fn, *args, **kwargs):
    """
    Run `fn(*args, **kwargs)` against the Gemini API while:
      1. respecting the shared per-minute budget across all workers
      2. retrying on 429 RESOURCE_EXHAUSTED, sleeping for the server-supplied
         retryDelay (with jitter) up to GEMINI_MAX_RETRIES attempts.

    Re-raises the final 429 (or any non-429 error) so callers' existing
    try/except blocks behave the same as before.
    """
    max_retries = _setting("GEMINI_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
    backoff = _INITIAL_BACKOFF

    for attempt in range(1, max_retries + 1):
        if not _acquire_slot():
            raise RuntimeError("Gemini rate-limit slot unavailable")

        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            if attempt >= max_retries:
                logger.error(
                    "Gemini still 429ing after %s attempts; giving up: %s",
                    attempt, exc,
                )
                raise
            wait = min(_retry_seconds(exc, fallback=backoff), 60.0)
            wait += random.uniform(0, 1.0)
            logger.warning(
                "Gemini 429 on attempt %s/%s; sleeping %.1fs and retrying.",
                attempt, max_retries, wait,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, _MAX_BACKOFF)
