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
    Never raises — callers always get a result dict.
    """
    try:
        return _fetch_follower_count_impl(instagram_url_or_handle)
    except Exception as e:
        logger.exception("fetch_follower_count fatal error: %s", e)
        return {
            "follower_count": None,
            "status": "error",
            "raw": {"error": str(e)},
        }


def _fetch_follower_count_impl(instagram_url_or_handle: str) -> Dict[str, Any]:
    if not instagram_url_or_handle:
        return {"follower_count": None, "status": "not_found", "raw": None}

    token = getattr(settings, "APIFY_TOKEN", None) or ""
    if not token:
        logger.warning("APIFY_TOKEN is not set; skipping Instagram scrape")
        return {
            "follower_count": None,
            "status": "skipped_no_token",
            "raw": None,
        }

    try:
        handle = normalize_instagram_handle(instagram_url_or_handle)
    except Exception as e:
        logger.warning("normalize_instagram_handle raised: %s", e, exc_info=True)
        return {"follower_count": None, "status": "not_found", "raw": None}

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
            logger.info(
                "Apify Instagram HTTP status=%s handle=%s content_length=%s",
                resp.status_code,
                handle,
                len(resp.content or b""),
            )
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                logger.warning("Apify Instagram scrape failed: %s", last_error)
                if attempt == 0:
                    time.sleep(1.5)
                continue
            try:
                data = resp.json()
            except ValueError:
                logger.warning(
                    "Apify Instagram response not JSON handle=%s body_prefix=%s",
                    handle,
                    (resp.text or "")[:300],
                )
                last_error = "invalid JSON from Apify"
                if attempt == 0:
                    time.sleep(1.5)
                continue
            try:
                result = _parse_apify_items(data, handle)
            except Exception:
                logger.exception("_parse_apify_items crashed handle=%s", handle)
                return {
                    "follower_count": None,
                    "status": "error",
                    "raw": {"error": "parse_failed"},
                }
            if not isinstance(result, dict):
                logger.error(
                    "_parse_apify_items returned %s for handle=%s",
                    type(result).__name__,
                    handle,
                )
                return {
                    "follower_count": None,
                    "status": "error",
                    "raw": None,
                }
            logger.info(
                "Apify Instagram parsed handle=%s status=%s follower_count=%s",
                handle,
                result.get("status"),
                result.get("follower_count"),
            )
            return result
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


def _coerce_apify_dataset_items(data: Any) -> list:
    """Normalize run-sync-get-dataset-items body to a list of row dicts."""
    try:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "data", "datasetItems", "defaultDatasetItems"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
            if any(
                k in data
                for k in ("username", "followersCount", "followers", "edge_followed_by")
            ):
                return [data]
    except Exception:
        logger.warning("Could not coerce Apify dataset items", exc_info=True)
    return []


def _parse_int_followers(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        try:
            i = int(value)
            return i if i >= 0 else None
        except (TypeError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", "").replace(" ", "")
    try:
        i = int(float(s))
        return i if i >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_apify_items(data: Any, handle: str) -> Dict[str, Any]:
    """Apify run-sync-get-dataset-items usually returns a JSON array of dataset items."""
    try:
        items = _coerce_apify_dataset_items(data)
        if len(items) == 0:
            logger.warning(
                "Apify Instagram empty dataset handle=%s raw_type=%s keys=%s",
                handle,
                type(data).__name__,
                list(data.keys()) if isinstance(data, dict) else None,
            )
            return {"follower_count": None, "status": "not_found", "raw": data}

        row = items[0]
        if not isinstance(row, dict):
            return {"follower_count": None, "status": "error", "raw": data}

        if row.get("private") is True:
            return {"follower_count": None, "status": "private", "raw": row}

        edge = row.get("edge_followed_by")
        try:
            edge_count = edge.get("count") if isinstance(edge, dict) else None
        except Exception:
            edge_count = None

        followers = (
            row.get("followersCount")
            or row.get("followers")
            or row.get("followerCount")
            or row.get("subscribersCount")
            or edge_count
        )

        if followers is None:
            uname = row.get("username") or row.get("id")
            if not uname:
                logger.warning(
                    "Apify Instagram no follower field handle=%s row_keys=%s",
                    handle,
                    list(row.keys())[:40],
                )
                return {"follower_count": None, "status": "not_found", "raw": row}
            followers = (
                row.get("followersCount")
                or row.get("followers")
                or row.get("followerCount")
            )

        count = _parse_int_followers(followers)

        if count is None:
            logger.warning(
                "Apify Instagram could not parse follower count handle=%s raw=%r row_keys=%s",
                handle,
                followers,
                list(row.keys())[:40],
            )
            return {"follower_count": None, "status": "not_found", "raw": row}

        return {"follower_count": count, "status": "ok", "raw": row}
    except Exception as e:
        logger.exception("_parse_apify_items error handle=%s", handle)
        return {
            "follower_count": None,
            "status": "error",
            "raw": {"error": str(e), "handle": handle},
        }
