"""
Sanitization, placeholder validation, and rendering for custom transactional email HTML.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from django.utils.html import escape

from quickstart.utils.email_branding_constants import (
    ALL_EMAIL_TYPE_KEYS,
    BRANDING_MODE_HTML,
    EMAIL_BRANDING_HTML_MAX_BYTES,
    PLACEHOLDER_REQUIREMENTS,
)

# {{ word_chars }} — single placeholder token
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def extract_placeholder_names(html: str) -> Set[str]:
    if not html:
        return set()
    return set(_PLACEHOLDER_RE.findall(html))


def sanitize_email_custom_html(html: str) -> str:
    """Strip script tags and inline event handlers from user HTML."""
    if not html or not isinstance(html, str):
        return ""
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", html)
    # Remove common event-handler attributes (best-effort; email clients ignore JS anyway)
    s = re.sub(
        r'(?i)\s+on[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
        "",
        s,
    )
    return s


def validate_branding_custom_html_dict(
    custom_html: Optional[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """
    Validate custom_html when mode is html. Returns (ok, list of error messages).
    """
    errors: List[str] = []
    if not custom_html:
        return True, []
    if not isinstance(custom_html, dict):
        return False, ["custom_html must be an object mapping email types to HTML strings."]
    for key, raw in custom_html.items():
        if key not in ALL_EMAIL_TYPE_KEYS:
            errors.append(f"Unknown email template key: {key!r}.")
            continue
        if raw is None or raw == "":
            continue
        if not isinstance(raw, str):
            errors.append(f"custom_html[{key!r}] must be a string.")
            continue
        encoded = raw.encode("utf-8")
        if len(encoded) > EMAIL_BRANDING_HTML_MAX_BYTES:
            errors.append(
                f"custom_html[{key!r}] exceeds maximum size of {EMAIL_BRANDING_HTML_MAX_BYTES} bytes."
            )
        req = PLACEHOLDER_REQUIREMENTS.get(key, {}).get("required", frozenset())
        present = extract_placeholder_names(raw)
        missing = sorted(req - present)
        if missing:
            errors.append(
                f"custom_html[{key!r}] is missing required placeholders: {', '.join('{{' + m + '}}' for m in missing)}"
            )
    return len(errors) == 0, errors


def validate_builder_mode_branding(branding: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Light validation for visual builder fields."""
    errors: List[str] = []
    if not isinstance(branding, dict):
        return False, ["Invalid branding payload."]
    mode = branding.get("mode", "builder")
    if mode not in ("builder", "html"):
        errors.append("mode must be 'builder' or 'html'.")
    for color_key in ("primary_color", "background_color"):
        v = branding.get(color_key)
        if v is None or v == "":
            continue
        if not isinstance(v, str):
            errors.append(f"{color_key} must be a string.")
            continue
        s = v.strip()
        if not re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", s) and not re.match(
            r"^rgb\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)$", s
        ):
            errors.append(f"{color_key} must be a valid hex (#RGB or #RRGGBB) or rgb() color.")
    ft = branding.get("footer_text")
    if ft is not None and isinstance(ft, str) and len(ft) > 300:
        errors.append("footer_text must be at most 300 characters.")
    cm = branding.get("confirmation_message")
    if cm is not None and isinstance(cm, str) and len(cm) > 500:
        errors.append("confirmation_message must be at most 500 characters.")
    btn = branding.get("button_text")
    if btn is not None and isinstance(btn, str) and len(btn) > 80:
        errors.append("button_text must be at most 80 characters.")
    return len(errors) == 0, errors


def normalize_and_validate_branding_payload(branding: Dict[str, Any]) -> Tuple[bool, List[str], Dict[str, Any]]:
    """
    Sanitize HTML templates, run validation, return (ok, errors, cleaned dict).
    Mutates a copy of branding for sanitized custom_html.
    """
    errors: List[str] = []
    if not isinstance(branding, dict):
        return False, ["Branding must be a JSON object."], {}
    out = dict(branding)
    ok_b, berr = validate_builder_mode_branding(out)
    errors.extend(berr)
    mode = out.get("mode") or BRANDING_MODE_BUILDER
    if mode == BRANDING_MODE_HTML:
        ch = out.get("custom_html")
        if ch is not None:
            if not isinstance(ch, dict):
                errors.append("custom_html must be an object.")
            else:
                cleaned: Dict[str, str] = {}
                for k, v in ch.items():
                    if k not in ALL_EMAIL_TYPE_KEYS:
                        continue
                    if v is None or v == "":
                        cleaned[k] = ""
                    elif isinstance(v, str):
                        cleaned[k] = sanitize_email_custom_html(v)
                    else:
                        errors.append(f"custom_html[{k!r}] must be a string.")
                out["custom_html"] = cleaned
                vok, verr = validate_branding_custom_html_dict(cleaned)
                if not vok:
                    errors.extend(verr)
    return len(errors) == 0, errors, out


def render_custom_html_template(html: str, placeholder_map: Dict[str, str]) -> str:
    """
    Replace {{key}} occurrences with escaped values (values are treated as plain text;
    caller may pass pre-rendered safe HTML fragments only from trusted sources).
    """
    if not html:
        return ""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = placeholder_map.get(key)
        if val is None:
            return m.group(0)
        return escape(str(val))

    return _PLACEHOLDER_RE.sub(repl, html)


def wrap_email_html_fragment_if_needed(html: str, title: str = "Email") -> str:
    """If HTML does not look like a full document, wrap in a minimal shell."""
    s = (html or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("<!doctype") or "<html" in low[:200]:
        return s
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>{escape(title)}</title></head><body style="margin:0;padding:24px;font-family:Arial,sans-serif;background:#f5f5f7;">{s}</body></html>"""
