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

_ALLOWED_ICONS = frozenset(
    {
        "BookOpen",
        "Utensils",
        "Sparkles",
        "Users",
        "MapPin",
        "Clock",
        "Heart",
        "ShieldCheck",
        "Gift",
        "Info",
    }
)


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
            logger.error("GEMINI_API_KEY is missing; cannot format description.")
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

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

        if response is None or not response.text:
            logger.warning("DescriptionFormatter: empty response for class %s", cls.pk)
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        parsed = self._parse_json(response.text)
        if not parsed:
            ClassesMain.objects.filter(pk=cls.pk).update(description_ai_status="failed")
            return

        summary = str(parsed.get("summary") or "").strip()[:240]
        sections_raw = parsed.get("sections") or []
        sections = self._normalize_sections(sections_raw)

        ClassesMain.objects.filter(pk=cls.pk).update(
            description_summary=summary,
            description_sections=sections,
            description_ai_source_hash=source_hash,
            description_ai_status="ready",
            description_ai_generated_at=timezone.now(),
        )

    def _build_prompt(self, description: str) -> str:
        return f"""You are restructuring a class description for display on a booking site.

Original description:
{description}

Return strict JSON only (no markdown fences):
{{
  "summary": "<= 160 characters, one engaging sentence for under the class title; plain text; do not start with an emoji>",
  "sections": [
    {{"title": "...", "icon": "<lucide name>", "body": "<plain text; use lines starting with '- ' for bullets>"}}
  ]
}}

Rules:
- Produce 3 to 7 sections in a logical order (e.g. Overview, What you'll do, Menu, What's included, Who it's for, Class details, Policies).
- Section titles: max 32 characters, no emojis.
- Preserve factual content: prices, addresses, durations, ages, allergens, ticket rules, ingredients.
- Remove decorative emojis and fluff where possible without losing meaning.
- Icon must be exactly one of: BookOpen, Utensils, Sparkles, Users, MapPin, Clock, Heart, ShieldCheck, Gift, Info.
"""

    def _parse_json(self, raw_text: str) -> dict | None:
        if not raw_text or not raw_text.strip():
            return None
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            fixed = re.sub(r",\s*([}\]])", r"\1", text)
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.warning("DescriptionFormatter: JSON parse failed")
            return None

    def _normalize_sections(self, raw_sections: Any) -> List[dict[str, Any]]:
        if not isinstance(raw_sections, list):
            return []
        out: List[dict[str, Any]] = []
        for i, sec in enumerate(raw_sections):
            if not isinstance(sec, dict):
                continue
            title = str(sec.get("title") or "Details").strip()[:32]
            icon = str(sec.get("icon") or "Info").strip()
            if icon not in _ALLOWED_ICONS:
                icon = "Info"
            body = str(sec.get("body") or "").strip()
            sid = slugify(title) or f"section-{i}"
            out.append(
                {
                    "id": sid,
                    "title": title,
                    "icon": icon,
                    "body": body,
                }
            )
        return out[:12]


def description_source_hash(description: str) -> str:
    raw = (description or "").strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
