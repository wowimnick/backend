"""
Versioned email marketing drag-and-drop document schema and HTML renderer.

Frontend must send the same `schema_version` and block shapes; see
`classeasily-frontend-next/.../marketing/builder/schema.js` for the canonical contract.
"""
from __future__ import annotations

import html
import re
import uuid
from typing import Any, Dict, List, Optional, Union

MARKETING_BUILDER_SCHEMA_VERSION = 1

JsonDict = Dict[str, Any]
Block = Dict[str, Any]


def default_builder_document() -> JsonDict:
    """Empty canvas matching schema v1."""
    return {
        "schema_version": MARKETING_BUILDER_SCHEMA_VERSION,
        "blocks": [
            {
                "id": str(uuid.uuid4()),
                "type": "text",
                "props": {
                    "content": "<p>Hi {{first_name}},</p><p>Your message here.</p>",
                    "align": "left",
                    "fontSize": 16,
                    "color": "#333333",
                },
            }
        ],
    }


def _esc(s: Optional[str]) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _style_fragment(props: JsonDict, *keys: str) -> str:
    parts = []
    for k in keys:
        v = props.get(k)
        if v is None or v == "":
            continue
        css_key = re.sub(r"([A-Z])", r"-\1", k).lower().lstrip("-")
        parts.append(f"{css_key}:{v}" if isinstance(v, (int, float)) else f"{css_key}:{_esc(str(v))}")
    return ";".join(parts)


def _render_block(block: Union[Block, None]) -> str:
    if not block or not isinstance(block, dict):
        return ""
    btype = (block.get("type") or "text").strip().lower()
    props = block.get("props") if isinstance(block.get("props"), dict) else {}
    children = block.get("children")

    if btype == "text":
        raw = props.get("content") or ""
        align = _esc(props.get("align") or "left")
        fs = props.get("fontSize")
        color = props.get("color")
        st = f"text-align:{align};"
        if fs:
            st += f"font-size:{int(fs)}px;"
        if color:
            st += f"color:{_esc(str(color))};"
        # Allow limited HTML from builder; strip scripts again at document level
        inner = str(raw) if isinstance(raw, str) else ""
        return f'<div class="ce-mk-block ce-mk-text" style="{_esc(st)}">{inner}</div>'

    if btype == "image":
        src = props.get("src") or ""
        alt = props.get("alt") or ""
        w = props.get("width")
        align = _esc(props.get("align") or "center")
        if not src or not isinstance(src, str):
            return ""
        st = f"text-align:{align};max-width:100%;"
        img_st = "max-width:100%;height:auto;display:inline-block;"
        if w:
            try:
                img_st += f"width:{int(w)}px;"
            except (TypeError, ValueError):
                pass
        return (
            f'<div class="ce-mk-block ce-mk-image" style="{_esc(st)}">'
            f'<img src="{_esc(src)}" alt="{_esc(alt)}" style="{_esc(img_st)}" />'
            "</div>"
        )

    if btype == "button":
        label = props.get("label") or "Click here"
        href = props.get("href") or "#"
        align = _esc(props.get("align") or "center")
        bg = _esc(str(props.get("backgroundColor") or "#111827"))
        fg = _esc(str(props.get("textColor") or "#ffffff"))
        st_wrap = f"text-align:{align};margin:16px 0;"
        st_btn = (
            f"display:inline-block;padding:12px 24px;background-color:{bg};"
            f"color:{fg};text-decoration:none;border-radius:6px;font-weight:600;"
        )
        return (
            f'<div class="ce-mk-block ce-mk-button" style="{_esc(st_wrap)}">'
            f'<a href="{_esc(str(href))}" style="{_esc(st_btn)}">{_esc(str(label))}</a>'
            "</div>"
        )

    if btype == "divider":
        color = _esc(str(props.get("color") or "#e5e7eb"))
        thick = props.get("thickness") or 1
        try:
            t = int(thick)
        except (TypeError, ValueError):
            t = 1
        return (
            f'<div class="ce-mk-block ce-mk-divider" style="margin:20px 0;">'
            f'<hr style="border:none;border-top:{t}px solid {color};margin:0;" />'
            "</div>"
        )

    if btype == "spacer":
        h = props.get("height") or 24
        try:
            height = int(h)
        except (TypeError, ValueError):
            height = 24
        return f'<div class="ce-mk-block ce-mk-spacer" style="height:{height}px;line-height:{height}px;">&nbsp;</div>'

    if btype == "section":
        bg = props.get("backgroundColor") or "#ffffff"
        py = props.get("paddingY", 24)
        px = props.get("paddingX", 16)
        try:
            py, px = int(py), int(px)
        except (TypeError, ValueError):
            py, px = 24, 16
        inner_html = _render_blocks_list(children if isinstance(children, list) else [])
        st = f"background-color:{_esc(str(bg))};padding:{py}px {px}px;border-radius:8px;"
        return f'<div class="ce-mk-block ce-mk-section" style="{_esc(st)}">{inner_html}</div>'

    if btype == "columns":
        cols = children if isinstance(children, list) else []
        gap = props.get("gap", 16)
        try:
            gap = int(gap)
        except (TypeError, ValueError):
            gap = 16
        widths = props.get("widths")
        if not isinstance(widths, list) or len(widths) != len(cols):
            widths = [100.0 / max(len(cols), 1)] * len(cols) if cols else [100.0]
        cells = []
        for i, col_blocks in enumerate(cols):
            if not isinstance(col_blocks, list):
                col_blocks = []
            w = widths[i] if i < len(widths) else 0
            try:
                pct = float(w)
            except (TypeError, ValueError):
                pct = 0
            inner = _render_blocks_list(col_blocks)
            cells.append(
                f'<td style="vertical-align:top;width:{pct}%;padding:0 {gap // 2}px;">{inner}</td>'
            )
        row = f'<tr>{"".join(cells)}</tr>' if cells else ""
        return (
            f'<div class="ce-mk-block ce-mk-columns" style="margin:12px 0;">'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="border-collapse:collapse;">{row}</table></div>'
        )

    return ""


def _render_blocks_list(blocks: List[Any]) -> str:
    if not blocks:
        return ""
    return "".join(_render_block(b) if isinstance(b, dict) else "" for b in blocks)


def normalize_builder_document(data: Any) -> JsonDict:
    """Ensure schema_version and blocks list exist."""
    if not isinstance(data, dict):
        return default_builder_document()
    ver = data.get("schema_version")
    if ver != MARKETING_BUILDER_SCHEMA_VERSION:
        # Upgrade path: v1 is the only version; unknown -> wrap as single text block
        if ver is None and data.get("blocks"):
            out = dict(data)
            out["schema_version"] = MARKETING_BUILDER_SCHEMA_VERSION
            return out
        return default_builder_document()
    blocks = data.get("blocks")
    if not isinstance(blocks, list):
        return default_builder_document()
    return {"schema_version": MARKETING_BUILDER_SCHEMA_VERSION, "blocks": blocks}


def render_builder_to_html(builder_json: Any) -> str:
    """Render builder document to an HTML fragment (wrapped for email clients)."""
    doc = normalize_builder_document(builder_json)
    inner = _render_blocks_list(doc.get("blocks") or [])
    return (
        f'<div class="ce-marketing-email" style="font-family:system-ui,-apple-system,BlinkMacSystemFont,'
        f'"Segoe UI",sans-serif;line-height:1.5;color:#333;max-width:600px;margin:0 auto;">{inner}</div>'
    )


def resolve_campaign_html_body(campaign) -> str:
    """Return HTML body for a BusinessEmailCampaign (builder or raw)."""
    ct = (getattr(campaign, "content_type", None) or "html").strip().lower()
    if ct == "builder_json":
        bj = getattr(campaign, "builder_json", None) or {}
        html_out = render_builder_to_html(bj)
        if html_out.strip():
            return html_out
    return getattr(campaign, "html_body", None) or ""
