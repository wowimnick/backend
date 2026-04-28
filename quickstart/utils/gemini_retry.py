import logging
import random
import time

from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)


def generate_content_with_retry(
    client,
    *,
    model,
    contents,
    config=None,
    max_attempts=4,
    base_delay_seconds=1.0,
    max_delay_seconds=10.0,
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

            if not is_retryable or attempt >= max_attempts:
                raise

            backoff = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            jitter = random.uniform(0, backoff * 0.25)
            sleep_seconds = backoff + jitter
            logger.warning(
                "Gemini call failed with status %s (attempt %s/%s). Retrying in %.2fs.",
                status_code,
                attempt,
                max_attempts,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
