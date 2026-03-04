import requests
import logging
from django.conf import settings
from typing import Optional

logger = logging.getLogger(__name__)


def trigger_nextjs_revalidation(
    path: Optional[str] = None, tag: Optional[str] = None
) -> bool:
    """
    Sends a request to the Next.js app to revalidate a path or tag.

    Args:
        path: The path to revalidate (e.g., "/classes/pottery-class")
        tag: The tag to revalidate (e.g., "category-pottery")

    Returns:
        bool: True if revalidation was successful, False otherwise
    """
    # Validate configuration
    if not settings.FRONTEND_BASE_URL:
        logger.warning("FRONTEND_BASE_URL not configured. Skipping revalidation.")
        return False

    if not settings.REVALIDATION_SECRET:
        logger.warning("REVALIDATION_SECRET not configured. Skipping revalidation.")
        return False

    if not path and not tag:
        logger.warning("Neither path nor tag provided for revalidation. Skipping.")
        return False

    # Skip when frontend URL is localhost and unreachable (e.g. backend in Docker, frontend not running).
    # Set FRONTEND_URL to your real frontend URL in staging/prod so revalidation works.
    base = (settings.FRONTEND_BASE_URL or "").strip().lower()
    if "localhost" in base or "127.0.0.1" in base:
        logger.debug(
            "Revalidation skipped (FRONTEND_BASE_URL is localhost). "
            "Set FRONTEND_URL to your frontend URL when deploying."
        )
        return False

    # Construct request
    base_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/api/revalidate"
    headers = {"Content-Type": "application/json"}
    payload = {"secret": settings.REVALIDATION_SECRET}

    # Add Vercel bypass token if configured (for staging/protected deployments)
    if (
        hasattr(settings, "VERCEL_AUTOMATION_BYPASS_SECRET")
        and settings.VERCEL_AUTOMATION_BYPASS_SECRET
    ):
        headers["x-vercel-protection-bypass"] = settings.VERCEL_AUTOMATION_BYPASS_SECRET
        logger.debug("Using Vercel bypass token for revalidation")

    if path:
        payload["type"] = "path"
        payload["path"] = path
        revalidation_target = f"path: {path}"
    elif tag:
        payload["type"] = "tag"
        payload["tag"] = tag
        revalidation_target = f"tag: {tag}"

    try:
        response = requests.post(
            base_url,
            json=payload,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()

        logger.info(f"Successfully triggered revalidation for {revalidation_target}")
        return True

    except requests.exceptions.Timeout:
        logger.error(f"Timeout while revalidating {revalidation_target}")
        return False

    except requests.exceptions.HTTPError as e:
        logger.error(
            f"HTTP error during revalidation for {revalidation_target}: "
            f"{e.response.status_code} - {e.response.text}"
        )
        return False

    except requests.exceptions.RequestException as e:
        logger.error(
            f"Error triggering Next.js revalidation for {revalidation_target}: {e}"
        )
        return False


def trigger_multiple_revalidations(
    paths: list[str] = None, tags: list[str] = None
) -> dict:
    """
    Trigger multiple revalidations in a single call.

    Args:
        paths: List of paths to revalidate
        tags: List of tags to revalidate

    Returns:
        dict: Summary of successful and failed revalidations
    """
    results = {"successful": [], "failed": [], "total": 0}

    if paths:
        for path in paths:
            results["total"] += 1
            if trigger_nextjs_revalidation(path=path):
                results["successful"].append(path)
            else:
                results["failed"].append(path)

    if tags:
        for tag in tags:
            results["total"] += 1
            if trigger_nextjs_revalidation(tag=tag):
                results["successful"].append(tag)
            else:
                results["failed"].append(tag)

    logger.info(
        f"Revalidation batch complete: {len(results['successful'])}/{results['total']} successful"
    )

    return results


# Tag constants for consistent revalidation across the platform
REVALIDATION_TAGS = {
    "classes_search": "classes-search",
    "homepage_content": "homepage-content",
    "homepage_classes": "homepage-classes",
    "classes": "classes",
    "businesses_list": "businesses-list",
    "business_categories": "business-categories",
    "categories": "categories",
    "collections": "collections",
}
