"""
Fallback membership for curated "experience themes" collections.

Runs after Gemini-based `CollectionAutoAssigner.process_class()` so classes that earned
zero matches against the preset thematic set still receive at least one link (keyword
overlap on title + description first, otherwise the ordered default collection).
"""

from __future__ import annotations

import logging
import re
from quickstart.models import ClassCollection, ClassesMain

logger = logging.getLogger(__name__)

_TOKEN_SPLIT = re.compile(r"[^\w\s-]+")


def _class_text_snapshot(cls: ClassesMain) -> str:
    parts = [
        getattr(cls, "title", "") or "",
        getattr(cls, "description", "") or "",
    ]
    return " ".join(parts).strip().lower()


def _keyword_sheet(col: ClassCollection) -> list[str]:
    aliases = col.search_aliases if isinstance(col.search_aliases, list) else []
    hay: list[str] = []
    nm = str(col.name or "").strip().lower()
    if nm:
        hay.append(nm)
    slug_text = str(col.slug or "").replace("-", " ").replace("_", " ").strip().lower()
    if slug_text and slug_text not in hay:
        hay.append(slug_text)
    for a in aliases:
        s = str(a).strip().lower()
        if s and len(s) > 2 and s not in hay:
            hay.append(s)

    uniq: list[str] = []
    seen: set[str] = set()
    for h in hay:
        if h not in seen:
            uniq.append(h)
            seen.add(h)
    uniq.sort(key=lambda s: (-len(s), s))
    return uniq


def _best_keyword_hit(text_lower: str, col: ClassCollection) -> tuple[int, ClassCollection]:
    keywords = _keyword_sheet(col)
    best = 0
    tokens = {_TOKEN_SPLIT.sub("", tok) for tok in text_lower.replace("/", " ").split()}
    tokens.discard("")
    for kw in keywords:
        if kw in text_lower:
            score = len(kw) + 40
            if score > best:
                best = score
            continue
        kwt = {_TOKEN_SPLIT.sub("", tok) for tok in kw.split()}
        kwt.discard("")
        if kwt & tokens:
            score = sum(len(t) for t in kwt & tokens)
            if score > best:
                best = score
    return best, col


def ensure_preset_collection_memberships(
    thematic_collection_ids_ordered: list[int],
) -> dict[str, int]:
    """
    Guarantee every ACTIVE class touches at least one collection in thematic_collection_ids_ordered.

    Does not remove existing links. Only `.add`s when missing all preset-collection IDs.

    Returns counts for logging/diagnostics.
    """
    cleaned = [cid for cid in thematic_collection_ids_ordered if isinstance(cid, int)]
    if not cleaned:
        logger.warning(
            "ensure_preset_collection_memberships: empty thematic_collection_ids_ordered; skipping"
        )
        return {"skipped": 1}

    cols = list(
        ClassCollection.objects.filter(
            pk__in=cleaned, type="automated", is_active=True
        )
    )
    if not cols:
        logger.warning(
            "ensure_preset_collection_memberships: no active automated collections matched ids %s",
            cleaned,
        )
        return {"skipped_collections": len(cleaned)}

    order_index = {pk: idx for idx, pk in enumerate(cleaned)}
    themed_sorted = sorted(cols, key=lambda c: order_index.get(c.pk, 9999))
    id_set = {c.pk for c in themed_sorted}
    default_col = themed_sorted[0]

    already_ok = fallback_kw = fallback_default = 0

    for cls in ClassesMain.objects.filter(status="active").iterator():
        if cls.collections.filter(pk__in=id_set).exists():
            already_ok += 1
            continue

        tl = _class_text_snapshot(cls)
        best_score = -1
        winner: ClassCollection | None = None
        if tl:
            for col in themed_sorted:
                score, _ = _best_keyword_hit(tl, col)
                if score > best_score:
                    best_score = score
                    winner = col

        if winner and best_score > 0:
            cls.collections.add(winner)
            fallback_kw += 1
            logger.info(
                "Preset coverage (keyword fallback): added class pk=%s to collection pk=%s (%r)",
                cls.pk,
                winner.pk,
                winner.name,
            )
        elif default_col is not None:
            cls.collections.add(default_col)
            fallback_default += 1
            logger.warning(
                "Preset coverage (no keyword overlap): defaulted class pk=%s %r onto collection pk=%s (%r)",
                cls.pk,
                getattr(cls, "title", ""),
                default_col.pk,
                default_col.name,
            )

    out = {
        "active_classes_already_had_preset": already_ok,
        "keyword_fallback_linked": fallback_kw,
        "default_fallback_linked": fallback_default,
        "preset_collection_count": len(themed_sorted),
    }
    logger.info(
        "ensure_preset_collection_memberships DONE %s",
        out,
    )
    return out
