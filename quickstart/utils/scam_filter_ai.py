"""Gemini-based scam/spam classification for inbound booker messages."""

from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

logger = logging.getLogger(__name__)


def _candidate_models():
    configured_models = getattr(settings, "GEMINI_SCAM_MODELS", None)
    if isinstance(configured_models, str) and configured_models.strip():
        models = [m.strip() for m in configured_models.split(",") if m.strip()]
        if models:
            return models

    single_model = getattr(settings, "GEMINI_MODEL", None)
    if isinstance(single_model, str) and single_model.strip():
        return [single_model.strip()]

    return ["gemini-2.5-flash", "gemini-2.0-flash"]


def parse_scam_classifier_json(raw_text: str) -> dict | None:
    """
    Parse LLM JSON response robustly. Returns dict with is_scam, confidence, reason
    or None if unparseable.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue

    match = re.search(
        r"['\"]is_scam['\"]\s*:\s*(true|false)",
        text,
        re.IGNORECASE,
    )
    if match:
        result = {"is_scam": match.group(1).lower() == "true"}
        conf_match = re.search(r"['\"]confidence['\"]\s*:\s*([0-9.]+)", text)
        if conf_match:
            result["confidence"] = float(conf_match.group(1))
        reason_match = re.search(r"['\"]reason['\"]\s*:\s*['\"]([^'\"]*)['\"]", text)
        if reason_match:
            result["reason"] = reason_match.group(1)
        return result

    return None


def classify_message(
    text: str,
    *,
    business_name: str | None = None,
    sender_name: str | None = None,
    sender_email: str | None = None,
) -> dict:
    """
    Classify whether a message is scam/spam vs a genuine class/booking inquiry.

    Returns {"is_scam": bool, "confidence": float, "reason": str}.
    Raises on unrecoverable API errors (caller should fail-open).
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing in settings.")

    client = genai.Client(api_key=api_key)
    prompt = f"""
You are a moderation classifier for a class and experience booking platform.

Determine whether the message below is a GENUINE inquiry about classes, bookings, schedules,
or business services — versus SCAM, SPAM, phishing, mass marketing, SEO spam, or unrelated solicitation.

--- CONTEXT ---
Business: {business_name or "Unknown"}
Sender name: {sender_name or "Unknown"}
Sender email: {sender_email or "Unknown"}

--- MESSAGE ---
{text}

--- OUTPUT FORMAT ---
Return a single JSON object (no markdown):
{{
  "is_scam": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "Brief explanation"
}}
"""

    response = None
    candidate_models = _candidate_models()
    for idx, model_name in enumerate(candidate_models, start=1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            break
        except genai_errors.ClientError as model_error:
            status_code = getattr(model_error, "code", None)
            is_retryable = status_code in {404, 429, 500, 502, 503, 504}
            logger.warning(
                "Gemini scam model %s failed code=%s retryable=%s: %s",
                model_name,
                status_code,
                is_retryable,
                model_error,
            )
            if not is_retryable or idx == len(candidate_models):
                raise

    if not response or not response.text:
        raise RuntimeError("Gemini returned empty scam classification response.")

    parsed = parse_scam_classifier_json(response.text)
    if not parsed:
        raise RuntimeError(f"Could not parse scam classifier JSON: {response.text[:200]}")

    is_scam = bool(parsed.get("is_scam"))
    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0

    reason = str(parsed.get("reason") or "").strip()
    return {"is_scam": is_scam, "confidence": confidence, "reason": reason}
