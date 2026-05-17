"""
Fetch public Instagram follower counts via Apify (Instagram Profile Scraper).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_INSTAGRAM_HOSTS = ("instagram.com", "www.instagram.com")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def normalize_instagram_handle(instagram_url_or_handle: str) -> Optional[str]:
    """
    Turn a URL or bare handle into an Instagram username (no @).
    Returns None if input is empty or invalid.
    """
    if not instagram_url_or_handle:
        return None
    raw = str(instagram_url_or_handle).strip()
    if not raw:
        return None
    raw = raw.lstrip("@")
    if _HANDLE_RE.match(raw):
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    if host not in _INSTAGRAM_HOSTS:
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    first = path.split("/")[0]
    if first in ("explore", "reel", "reels", "p", "stories", "accounts"):
        return None
    if _HANDLE_RE.match(first):
        return first
    return None


def fetch_follower_count(instagram_url_or_handle: str) -> Dict[str, Any]:
    """
    Call Apify run-sync-get-dataset-items and parse the first profile row.

    Returns dict with keys: follower_count (int|None), status (str), raw (list|dict).
    status in: ok, not_found, private, error, skipped_no_token
    """
    token = getattr(settings, "APIFY_TOKEN", None) or ""
    if not token:
        logger.warning("APIFY_TOKEN is not set; skipping Instagram scrape")
        return {
            "follower_count": None,
            "status": "skipped_no_token",
            "raw": None,
        }

    handle = normalize_instagram_handle(instagram_url_or_handle)
    if not handle:
        return {"follower_count": None, "status": "not_found", "raw": None}

    url = (
        "https://api.apify.com/v2/acts/apify~instagram-profile-scraper/"
        "run-sync-get-dataset-items"
    )
    params = {"token": token}
    payload = {"usernames": [handle]}

    last_error: Optional[str] = None
    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                params=params,
                json=payload,
                timeout=60,
            )
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                logger.warning("Apify Instagram scrape failed: %s", last_error)
                if attempt == 0:
                    time.sleep(1.5)
                continue
            data = resp.json()
            return _parse_apify_items(data, handle)
        except requests.RequestException as e:
            last_error = str(e)
            logger.warning("Apify request error (attempt %s): %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(1.5)

    return {
        "follower_count": None,
        "status": "error",
        "raw": {"error": last_error},
    }


def _parse_apify_items(data: Any, handle: str) -> Dict[str, Any]:
    """Apify run-sync-get-dataset-items returns a JSON array of dataset items."""
    if not isinstance(data, list) or len(data) == 0:
        return {"follower_count": None, "status": "not_found", "raw": data}

    row = data[0]
    if not isinstance(row, dict):
        return {"follower_count": None, "status": "error", "raw": data}

    if row.get("private") is True:
        return {"follower_count": None, "status": "private", "raw": row}

    edge = row.get("edge_followed_by")
    edge_count = edge.get("count") if isinstance(edge, dict) else None
    followers = row.get("followersCount") or row.get("followers") or edge_count

    if followers is None:
        # Sometimes username mismatch / empty profile
        uname = row.get("username") or row.get("id")
        if not uname:
            return {"follower_count": None, "status": "not_found", "raw": row}
        followers = row.get("followersCount")

    try:
        count = int(followers) if followers is not None else None
    except (TypeError, ValueError):
        count = None

    if count is None or count < 0:
        return {"follower_count": None, "status": "not_found", "raw": row}

    return {"follower_count": count, "status": "ok", "raw": row}
