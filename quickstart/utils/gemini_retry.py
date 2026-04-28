import logging
import random
import time

from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)


def _truncate(value, limit=600):
    text = str(value) if value is not None else ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


def _extract_retry_after_seconds(exc):
    """
    Best-effort parse of Retry-After header from SDK exceptions.
    Returns float seconds or None.
    """
    response = getattr(exc, "response", None)
    if not response:
        return None

    headers = getattr(response, "headers", None)
    if not headers:
        return None

    retry_after = headers.get("retry-after")
    if not retry_after:
        return None

    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


def _extract_error_context(exc):
    """
    Best-effort extraction of API error context from SDK exceptions.
    """
    response = getattr(exc, "response", None)
    context = {
        "status_code": getattr(exc, "code", None),
        "reason": getattr(exc, "message", None) or str(exc),
        "retry_after_seconds": None,
        "response_body": None,
    }

    if not response:
        return context

    context["status_code"] = getattr(response, "status_code", context["status_code"])
    context["retry_after_seconds"] = _extract_retry_after_seconds(exc)

    body = getattr(response, "text", None)
    if not body:
        content = getattr(response, "content", None)
        if content:
            body = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    context["response_body"] = _truncate(body)
    return context


def generate_content_with_retry(
    client,
    *,
    model,
    contents,
    config=None,
    max_attempts=5,
    base_delay_seconds=1.0,
    max_delay_seconds=20.0,
    retryable_statuses=None,
):
    """
    Call Gemini generate_content with bounded retries for transient failures.
    """
    if retryable_statuses is None:
        retryable_statuses = {429, 500, 502, 503, 504}

    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except (genai_errors.ClientError, genai_errors.ServerError) as exc:
            status_code = getattr(exc, "code", None)
            is_retryable = status_code in retryable_statuses
            error_context = _extract_error_context(exc)

            if not is_retryable or attempt >= max_attempts:
                logger.error(
                    "Gemini call failed permanently (model=%s attempt=%s/%s retryable=%s status=%s retry_after=%s reason=%s body=%s).",
                    model,
                    attempt,
                    max_attempts,
                    is_retryable,
                    error_context.get("status_code"),
                    error_context.get("retry_after_seconds"),
                    _truncate(error_context.get("reason")),
                    error_context.get("response_body"),
                )
                raise

            retry_after_seconds = error_context.get("retry_after_seconds")
            backoff = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            jitter = random.uniform(0, max(0.25, backoff * 0.25))
            sleep_seconds = retry_after_seconds if retry_after_seconds is not None else backoff + jitter

            logger.warning(
                "Gemini call failed (model=%s status=%s attempt=%s/%s). Retrying in %.2fs. reason=%s body=%s",
                model,
                error_context.get("status_code"),
                attempt,
                max_attempts,
                sleep_seconds,
                _truncate(error_context.get("reason"), 300),
                error_context.get("response_body"),
            )
            time.sleep(sleep_seconds)
