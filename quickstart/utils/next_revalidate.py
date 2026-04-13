"""
Notify the Next.js app to revalidate cache tags (see classeasily-frontend-next /api/revalidate).

settings.NEXT_REVALIDATE_URL is derived from FRONTEND_URL (e.g. staging vs prod Next host).
Set REVALIDATION_SECRET to match the Next.js REVALIDATION_SECRET env. If the secret is unset,
calls are no-ops.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def revalidate_next_cache_tags(tags: list[str]) -> None:
    """POST each tag to the Next.js revalidate endpoint (one request per tag)."""
    url = getattr(settings, "NEXT_REVALIDATE_URL", None) or ""
    secret = getattr(settings, "REVALIDATION_SECRET", None) or ""
    if not url or not secret:
        return
    if not tags:
        return

    payload_base = {"secret": secret, "type": "tag"}
    for tag in tags:
        if not tag:
            continue
        body = json.dumps({**payload_base, "tag": tag}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Next revalidate tag %r returned HTTP %s", tag, resp.status
                    )
        except urllib.error.HTTPError as e:
            logger.warning(
                "Next revalidate tag %r failed: HTTP %s %s",
                tag,
                e.code,
                e.reason,
            )
        except Exception as e:
            logger.warning("Next revalidate tag %r failed: %s", tag, e)
