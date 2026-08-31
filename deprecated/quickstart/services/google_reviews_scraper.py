"""
Fetch Google Maps reviews via Apify (compass/google-maps-reviews-scraper).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _normalize_maps_url(url: str) -> Optional[str]:
    if not url or not str(url).strip():
        return None
    raw = str(url).strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    if "google" not in host and "goo.gl" not in host:
        return None
    return raw


def _flatten_review_items(data: Any) -> List[Dict[str, Any]]:
    """Apify dataset may be a flat list of reviews or include nested structures."""
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("reviewId"):
            out.append(item)
            continue
        nested = item.get("reviews")
        if isinstance(nested, list):
            for r in nested:
                if isinstance(r, dict) and r.get("reviewId"):
                    out.append(r)
    return out


def fetch_reviews(
    google_maps_url: str,
    *,
    max_reviews: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Call Apify run-sync-get-dataset-items and return review dicts.

    Returns dict with keys: reviews (list), status (str), raw (Any).
    status in: ok, not_found, error, skipped_no_token
    """
    token = getattr(settings, "APIFY_TOKEN", None) or ""
    if not token:
        logger.warning("APIFY_TOKEN is not set; skipping Google reviews scrape")
        return {
            "reviews": [],
            "status": "skipped_no_token",
            "raw": None,
        }

    url_clean = _normalize_maps_url(google_maps_url)
    if not url_clean:
        return {"reviews": [], "status": "not_found", "raw": None}

    apify_url = (
        "https://api.apify.com/v2/acts/compass~google-maps-reviews-scraper/"
        "run-sync-get-dataset-items"
    )
    params = {"token": token}
    payload = {
        "startUrls": [{"url": url_clean}],
        "maxReviews": max_reviews if max_reviews is not None else 99999,
        "reviewsSort": "newest",
        "language": "en",
    }

    last_error: Optional[str] = None
    # Full refresh can take several minutes.
    timeout_sec = 600
    for attempt in range(2):
        try:
            resp = requests.post(
                apify_url,
                params=params,
                json=payload,
                timeout=timeout_sec,
            )
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                logger.warning("Apify Google reviews scrape failed: %s", last_error)
                if attempt == 0:
                    time.sleep(2.0)
                continue
            data = resp.json()
            reviews = _flatten_review_items(data)
            if not reviews:
                return {"reviews": [], "status": "not_found", "raw": data}
            return {"reviews": reviews, "status": "ok", "raw": data}
        except requests.RequestException as e:
            last_error = str(e)
            logger.warning(
                "Apify Google reviews request error (attempt %s): %s",
                attempt + 1,
                e,
            )
            if attempt == 0:
                time.sleep(2.0)

    return {
        "reviews": [],
        "status": "error",
        "raw": {"error": last_error},
    }
