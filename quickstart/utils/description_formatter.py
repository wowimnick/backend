"""
Gemini-powered restructuring of class descriptions into summary + collapsible sections.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, List

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from quickstart.models import ClassesMain

logger = logging.getLogger(__name__)

# Raw JSON text logged at INFO (raise logging level or use DEBUG for full body).
_GEMINI_RESPONSE_PREVIEW_CHARS = 6000
_SECTION_BODY_PREVIEW_CHARS = 400

def _preview(text: str, limit: int = _GEMINI_RESPONSE_PREVIEW_CHARS) -> str:
    """Truncated single-line-safe preview for logs."""
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return f"{s[:limit]}... [truncated total_len={len(s)}]"


class DescriptionFormatter:
    """Calls Gemini to produce summary + structured sections JSON."""

    @staticmethod
    def _candidate_models():
        configured_models = getattr(settings, "GEMINI_COLLECTION_MODELS", None)
        if isinstance(configured_models, str) and configured_models.strip():
            models = [m.strip() for m in configured_models.split(",") if m.strip()]
            if models:
                return models

        single_model = getattr(settings, "GEMINI_COLLECTION_MODEL", None) or getattr(
            settings, "GEMINI_MODEL", None
        )
        if isinstance(single_model, str) and single_model.strip():
            return [single_model.strip()]

        return ["gemini-2.5-flash", "gemini-2.0-flash"]

    def process(self, cls: ClassesMain, source_hash: str) -> None:
        raw = (cls.description or "").strip()
        if not raw:
            ClassesMain.objects.filter(pk=cls.pk).update(
                description_summary="",
                description_sections=[],
                description_ai_source_hash="",
                description_ai_status="ready",
                description_ai_generated_at=timezone.now(),
            )
            return

        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            logger.error(
                "DescriptionFormatter class_id=%s GEMINI_API_KEY missing; aborting.",
                cls.pk,
            )
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        logger.info(
            "DescriptionFormatter START class_id=%s slug=%r desc_chars=%s source_hash=%s",
            cls.pk,
            getattr(cls, "slug", None),
            len(raw),
            source_hash[:16] + "..." if len(source_hash) > 16 else source_hash,
        )

        client = genai.Client(api_key=api_key)
        prompt = self._build_prompt(raw)

        response = None
        candidate_models = self._candidate_models()
        for idx, model_name in enumerate(candidate_models, start=1):
            try:
                logger.info(
                    "DescriptionFormatter: model %s (%s/%s) class=%s",
                    model_name,
                    idx,
                    len(candidate_models),
                    cls.pk,
                )
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                )
                logger.info(
                    "DescriptionFormatter class_id=%s Gemini success model=%s",
                    cls.pk,
                    model_name,
                )
                break
            except genai_errors.ClientError as model_error:
                status_code = getattr(model_error, "code", None)
                is_retryable = status_code in {404, 429, 500, 502, 503, 504}
                logger.warning(
                    "Gemini model %s failed code=%s retryable=%s: %s",
                    model_name,
                    status_code,
                    is_retryable,
                    model_error,
                )
                if not is_retryable or idx == len(candidate_models):
                    raise

        if response is None:
            logger.error(
                "DescriptionFormatter class_id=%s no Gemini response (all models exhausted or none configured)",
                cls.pk,
            )
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        raw_response_text = (response.text or "").strip()
        logger.info(
            "DescriptionFormatter class_id=%s gemini_response_chars=%s preview=\n%s",
            cls.pk,
            len(raw_response_text),
            _preview(raw_response_text),
        )
        logger.debug(
            "DescriptionFormatter class_id=%s gemini_response_FULL=%s",
            cls.pk,
            raw_response_text,
        )

        if not raw_response_text:
            logger.warning(
                "DescriptionFormatter class_id=%s empty response.text after generate_content",
                cls.pk,
            )
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        parsed = self._parse_json(raw_response_text)
        if not parsed:
            logger.error(
                "DescriptionFormatter class_id=%s JSON parse FAILED raw_preview=\n%s",
                cls.pk,
                _preview(raw_response_text, 8000),
            )
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        logger.info(
            "DescriptionFormatter class_id=%s parsed_top_level_keys=%s",
            cls.pk,
            list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__,
        )

        summary = str(parsed.get("summary") or "").strip()[:240]
        sections_raw = parsed.get("sections") or []
        sections = self._normalize_sections(sections_raw)

        logger.info(
            "DescriptionFormatter class_id=%s GENERATED summary=%r summary_chars=%s sections_in=%s sections_out=%s",
            cls.pk,
            summary,
            len(summary),
            len(sections_raw) if isinstance(sections_raw, list) else "n/a",
            len(sections),
        )
        for i, sec in enumerate(sections):
            body = sec.get("body") or ""
            logger.info(
                "DescriptionFormatter class_id=%s section[%s] id=%r title=%r body_chars=%s body_preview=\n%s",
                cls.pk,
                i,
                sec.get("id"),
                sec.get("title"),
                len(str(body)),
                _preview(str(body), _SECTION_BODY_PREVIEW_CHARS),
            )

        ClassesMain.objects.filter(pk=cls.pk).update(
            description_summary=summary,
            description_sections=sections,
            description_ai_source_hash=source_hash,
            description_ai_status="ready",
            description_ai_generated_at=timezone.now(),
        )
        logger.info(
            "DescriptionFormatter DONE class_id=%s slug=%r DB updated status=ready summary_store_chars=%s sections_store=%s",
            cls.pk,
            getattr(cls, "slug", None),
            len(summary),
            len(sections),
        )

    def _build_prompt(self, description: str) -> str:
        return f"""You help hosts present class descriptions on a fun local-experience marketplace.

Rewrite for clarity and easy scanning, but sound HUMAN: warm, enthusiastic, and personal — never stiff, robotic, or corporate.

Original description:
{description}

Return strict JSON only (no markdown fences around the whole answer — the JSON itself must be raw):
{{
  "summary": "<= 240 characters. One inviting sentence for under the class title. Conversational; match the host's energy.>",
  "sections": [
    {{
      "title": "<exactly ONE Unicode emoji at the very start, then a short heading (keep the full title reasonable length, ~40 chars max). Example: \\"📖 Overview\\" or \\"🍳 What you'll make\\">",
      "body": "<section text. Use **double asterisks** for bold phrases. Use \\n for line breaks inside this JSON string — never put literal newline characters inside quoted values. Split ideas into paragraphs with \\n\\n; use lines starting with '- ' for bullet lists.>"
    }}
  ]
}}

Tone and content rules:
- Section titles use normal emojis only (one relevant emoji per title at the start). Do not output Lordicon URLs, Lucide icon names, or other icon systems.
- The FIRST section must be a short class overview / introduction so readers immediately understand what the class is. Put it first in the array.
- KEEP emojis that appear in the original whenever they still fit naturally (especially in section bodies). Do not strip personality for "professionalism."
- Do NOT over-condense. Section bodies should stay informative and readable: keep specifics, stories, and lists rather than boiling everything into vague one-liners.
- Format each section body for scanning: short paragraphs, blank lines between paragraphs, and bullet lines where lists make sense — avoid stuffing everything into a single paragraph when there are multiple distinct points.
- Preserve ALL factual details: prices, addresses, durations, ages, allergens, ticket rules, ingredients, what's included, cancellation policy — verbatim where reasonable.
- Organize into 4–8 sections in a sensible order after the opening overview (e.g. What to expect / Menu or agenda / What's included / Good to know / Policies).
"""

    def _parse_json(self, raw_text: str) -> dict | None:
        parsed = _parse_description_formatter_json(raw_text)
        if parsed is not None:
            return parsed
        text = (raw_text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        logger.warning(
            "DescriptionFormatter JSON parse failed after repairs raw_preview=\n%s",
            _preview(text, 8000),
        )
        return None

    def _normalize_sections(self, raw_sections: Any) -> List[dict[str, Any]]:
        if not isinstance(raw_sections, list):
            return []
        out: List[dict[str, Any]] = []
        for i, sec in enumerate(raw_sections):
            if not isinstance(sec, dict):
                continue
            title = str(sec.get("title") or "Details").strip()[:40]
            body = str(sec.get("body") or "").strip()
            sid = slugify(title) or f"section-{i}"
            out.append(
                {
                    "id": sid,
                    "title": title,
                    "body": body,
                }
            )
        return out[:12]


def _escape_control_chars_in_json_strings(text: str) -> str:
    """
    Gemini often returns pretty-printed bodies with literal newlines inside JSON
    string values, which is invalid JSON. Escape control chars only while inside
    double-quoted strings.
    """
    out: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            out.append("\\r")
            continue
        if in_string and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    return "".join(out)


def _try_parse_json_object(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_description_formatter_json(raw_text: str) -> dict | None:
    """Parse Gemini description JSON with common LLM formatting repairs."""
    if not raw_text or not raw_text.strip():
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    attempts = [
        text,
        re.sub(r",\s*([}\]])", r"\1", text),
        _escape_control_chars_in_json_strings(text),
        _escape_control_chars_in_json_strings(
            re.sub(r",\s*([}\]])", r"\1", text)
        ),
    ]
    seen: set[str] = set()
    for candidate in attempts:
        if candidate in seen:
            continue
        seen.add(candidate)
        parsed = _try_parse_json_object(candidate)
        if parsed is not None:
            return parsed
    return None


def description_source_hash(description: str) -> str:
    raw = (description or "").strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
