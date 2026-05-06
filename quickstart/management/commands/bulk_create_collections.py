"""
Create many ClassCollection rows in one shot without per-save signals.

Creates rows via bulk_create (no post_save), so inserting N collections does not
queue `update_trending_collections_task` N times.

For automated collections (`--automated`, default unless `--manual`):

- **One Gemini call** generates `description`, `ai_criteria`, and `search_aliases`
  for every name in your list (`--names` comma-separated).

- **One classification pass** optional at the end (`--sync-classify` or
  `--celery-classify`, default celery) runs `update_trending_collections_task`
  exactly once against all active classes (each class still gets its own Gemini
  curator call internally — unchanged from normal operations).

Manual mode (`--manual`): no Gemini, simple placeholder descriptions only.

Examples:

    python manage.py bulk_create_collections --preset experience-themes

    python manage.py bulk_create_collections \\
        --names "Baking, Candles, Chocolate, Cocktail, Cooking, Craft, Fabric, Floristry, Jewlery, Pottery"

    python manage.py bulk_create_collections \\
        --names "Wine Tasting,Kids Crafts" \\
        --no-gemini --sync-classify

    python manage.py bulk_create_collections Beer Cheese --manual
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils.text import slugify

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from quickstart.models import ClassCollection
from quickstart.utils.services import CollectionAutoAssigner
from quickstart.utils.experience_theme_coverage import (
    ensure_preset_collection_memberships,
)


logger = logging.getLogger(__name__)

# Curated onboarding bundle (requested): "I want" + black pills + coverage fallback helper.
PRESET_EXPERIENCE_THEME_NAMES = [
    "Baking",
    "Candles",
    "Chocolate",
    "Cocktail",
    "Cooking",
    "Craft",
    "Fabric",
    "Floristry",
    "Jewlery",
    "Pottery",
]
PRESET_EXPERIENCE_THEMES_LABEL = "experience-themes"
PRESET_THEME_CHIP_COLOR_HEX = "#000000"


def _parse_name_list(names_arg: str | None, positionals: list[str]) -> list[str]:
    parts: list[str] = []
    if names_arg:
        for chunk in names_arg.replace("\n", ",").split(","):
            s = chunk.strip()
            if s:
                parts.append(s)
    for p in positionals:
        s = str(p).strip()
        if s:
            parts.append(s)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for n in parts:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def _unique_slug(base: str, reserved: set[str]) -> str:
    """Ensure slug fits ClassCollection.slug (max_length=120); avoid collisions."""
    b = (base or "collection").strip().lower().strip("-") or "collection"
    if len(b) > 118:
        b = b[:118].rstrip("-")
    candidate = b
    n = 2
    while candidate in reserved:
        suffix = f"-{n}"
        root = b[: max(1, 120 - len(suffix))]
        candidate = f"{root}{suffix}".strip("-").lower()
        n += 1
    reserved.add(candidate)
    return candidate


def _heuristic_bundle(name: str) -> dict[str, Any]:
    n = name.strip()
    key = n.lower()
    aliases = {key}
    for token in key.replace("&", " and ").replace("/", " ").split():
        token = token.strip()
        if len(token) > 2:
            aliases.add(token)
    aliases.discard("")
    return {
        "description": (
            f"Hands-on {n.lower()} workshops, experiences, and classes near you."
        ),
        "ai_criteria": (
            f"If the class title or description clearly relates to {n.lower()} "
            f"(skills, crafts, tastings, maker experiences typical of '{n}', including "
            "common synonyms), include this collection."
        ),
        "search_aliases": sorted(aliases)[:12],
    }


def _gemini_bulk_profiles(
    names: list[str], *,
    curator_title_focus: bool = False,
) -> list[dict[str, Any]]:
    """
    Single Gemini round-trip returning description + ai_criteria + aliases per name.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise CommandError(
            "GEMINI_API_KEY is not set - use --no-gemini or configure the key."
        )

    client = genai.Client(api_key=api_key)

    enumerated = [{"index": i + 1, "name": n} for i, n in enumerate(names)]

    prompt = f"""
You are curating thematic collections on a marketplace for local in-person classes
and workshops (cooking, crafts, tastings, pottery, floristry, etc.).

Given this exact list of collection display names (keep spelling as given):
{json.dumps(enumerated, indent=2)}

For EACH name, output one object with keys:
- "name": same string exactly as provided (verbatim)
- "description": ONE short homepage card sentence, friendly and specific to that theme
- "ai_criteria": curator instructions paragraph for an LLM that only sees title+description text.
  Mention concrete keywords/synonyms, materials, verbs, WHO it is FOR, negative examples
  of what NOT to assign, and ambiguity rules.
- "search_aliases": 4 to 12 unique lowercased single-word-or-hyphen synonyms users might search
  (hyphenate phrases like cocktail-making).

Return STRICT JSON ONLY (no markdown), shape:
{{"collections": [<object per name in same order as list above>]}}

Important: Emit exactly {len(names)} objects in array "collections", same order.

"""
    if curator_title_focus:
        prompt += """
Extra rule for downstream automated matching:
Classifiers only receive class TITLE and DESCRIPTION. Write ai_criteria so obvious product nouns
and words in SHORT TITLES carry the most weight; treat long fluffy marketing prose as weaker signal.
Prefer concrete verbs, audience, materials, and synonyms that might appear verbatim in titles.
"""

    candidate_models = CollectionAutoAssigner._candidate_models()
    response = None
    last_error: Exception | None = None

    for idx, model_name in enumerate(candidate_models, start=1):
        try:
            logger.info(
                "bulk_create_collections: Gemini profiles model %s (%s/%s)",
                model_name,
                idx,
                len(candidate_models),
            )
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
            last_error = model_error
            logger.warning(
                "Gemini bulk profiles model %s failed code=%s retryable=%s: %s",
                model_name,
                status_code,
                is_retryable,
                model_error,
            )
            if not is_retryable or idx == len(candidate_models):
                raise CommandError(f"Gemini failed on all curator models: {last_error}") from last_error

    if response is None or not (response.text or "").strip():
        raise CommandError("Gemini returned an empty response for bulk profile generation.")

    text = response.text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        try:
            data = json.loads(fixed)
        except json.JSONDecodeError as e:
            raise CommandError(f"Could not parse Gemini JSON: {e}") from e

    items = data.get("collections")
    if not isinstance(items, list) or len(items) < 1:
        raise CommandError(
            'Gemini returned unexpected shape: expected non-empty JSON list "collections".'
        )

    by_lc: dict[str, dict[str, Any]] = {}
    for it in items:
        if isinstance(it, dict):
            key = str(it.get("name", "")).strip().lower()
            if key:
                by_lc[key] = it

    ordered_blobs: list[dict[str, Any]] = []
    for idx, name in enumerate(names):
        blob = by_lc.get(name.strip().lower(), {})
        if not blob:
            if idx < len(items) and isinstance(items[idx], dict):
                blob = items[idx]
                logger.warning(
                    "bulk_create_collections: no key match for %r — using positional index %s",
                    name,
                    idx + 1,
                )
            else:
                blob = {}

        aliases = blob.get("search_aliases")
        if not isinstance(aliases, list):
            aliases = []

        aliases_norm: list[str] = []
        seen_a: set[str] = set()
        for x in aliases:
            s = str(x).strip().lower()
            if s and s not in seen_a and len(s) < 121:
                seen_a.add(s)
                aliases_norm.append(s)

        desc = str(blob.get("description", "") or "").strip()
        crit = str(blob.get("ai_criteria", "") or "").strip()
        crit = crit[:16000]

        fallback = _heuristic_bundle(name)
        ordered_blobs.append(
            {
                "name": name,
                "description": desc or fallback["description"],
                "ai_criteria": crit or fallback["ai_criteria"],
                "search_aliases": aliases_norm or list(fallback["search_aliases"]),
            }
        )

    return ordered_blobs


def _schedule_revalidate() -> None:
    try:
        from quickstart.utils.next_revalidate import revalidate_next_cache_tags

        revalidate_next_cache_tags(
            ["collections", "homepage-content", "classes-search"]
        )
    except Exception as exc:
        logger.warning("Next.js revalidate skipped: %s", exc)


class Command(BaseCommand):
    help = (
        "Bulk-create ClassCollection rows using one Gemini call for automation copy, "
        "then optionally run classification once."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--names",
            dest="names",
            default=None,
            help=(
                'Comma-separated display names e.g. "Baking,Candles,Chocolate". '
                'You can also pass extra names as positional arguments.'
            ),
        )
        parser.add_argument(
            "remainder",
            nargs="*",
            help="Additional collection names (optional).",
        )
        parser.add_argument(
            "--manual",
            action="store_true",
            help="Manual collections only (no ai_criteria, no classify pass).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen without writing to the DB.",
        )
        parser.add_argument(
            "--no-gemini",
            action="store_true",
            help="Skip the single Gemini profiling call; fill description / ai_criteria heuristically.",
        )
        parser.add_argument(
            "--skip-existing",
            dest="skip_existing",
            action="store_true",
            default=True,
            help="Skip names whose slug already exists (default True).",
        )
        parser.add_argument(
            "--fail-on-existing",
            action="store_true",
            help="Exit non-zero when any slug would collide.",
        )
        parser.add_argument(
            "--skip-classify",
            action="store_true",
            help="Do not queue or run update_trending_collections_task afterwards.",
        )
        parser.add_argument(
            "--sync-classify",
            action="store_true",
            help="After commit run update_trending_collections_task synchronously (no Celery).",
        )
        parser.add_argument(
            "--celery-classify",
            action="store_true",
            help="Explicitly enqueue update_trending_collections_task via Celery (default for automated collections).",
        )
        parser.add_argument(
            "--preset",
            choices=[PRESET_EXPERIENCE_THEMES_LABEL],
            default=None,
            help=(
                f'"{PRESET_EXPERIENCE_THEMES_LABEL}" - fixed thematic list ({len(PRESET_EXPERIENCE_THEME_NAMES)} '
                'collections), show_in_i_want=ON, pill color black, chained coverage so no '
                "active class is missing all of those collections."
            ),
        )
        parser.add_argument(
            "--skip-theme-coverage",
            action="store_true",
            help="With --preset, skip chained keyword/default membership pass (Gemini classify only).",
        )
        parser.add_argument(
            "--show-in-i-want",
            action="store_true",
            dest="iwant_true",
            help="Turn on show_in_i_want (default OFF for scripted bulk inserts; ON for --preset).",
        )
        parser.add_argument(
            "--show-on-homepage-rows",
            action="store_true",
            dest="home_rows_true",
            help=(
                "(Reserved) show_on_homepage_rows is already True by default."
            ),
        )

    def handle(self, *args, **options):
        manual_flag = options.get("manual", False)
        automated = not manual_flag

        preset_choice = options.get("preset")
        skip_theme_coverage = options.get("skip_theme_coverage", False)
        run_theme_coverage = bool(
            preset_choice == PRESET_EXPERIENCE_THEMES_LABEL and not skip_theme_coverage
        )

        if preset_choice == PRESET_EXPERIENCE_THEMES_LABEL:
            if manual_flag:
                raise CommandError("--preset experience-themes cannot be used with --manual.")
            raw_names = None
            positionals: list[str] = []
            names_in = list(PRESET_EXPERIENCE_THEME_NAMES)
            self.stdout.write(
                self.style.NOTICE(
                    f"Using preset '{PRESET_EXPERIENCE_THEMES_LABEL}' ({len(names_in)} names); "
                    "--names / extra positionals are ignored."
                )
            )
            options["iwant_true"] = True
        else:
            raw_names = options["names"]
            positionals = list(options.get("remainder") or [])
            names_in = _parse_name_list(raw_names, positionals)

        if not names_in:
            raise CommandError(
                'Provide names via --names "A,B", positionals, or --preset experience-themes.'
            )

        chip_color = PRESET_THEME_CHIP_COLOR_HEX if preset_choice else ""
        dry_run = options["dry_run"]
        no_gemini = options["no_gemini"]
        skip_existing = options["skip_existing"]
        fail_existing = options["fail_on_existing"]
        skip_classify = options["skip_classify"]
        sync_classify = options["sync_classify"]
        iw = options["iwant_true"]

        existing_slugs = set(
            ClassCollection.objects.values_list("slug", flat=True)
        )

        collisions: list[str] = []
        to_slugify: list[str] = []

        reserved = set(existing_slugs)
        for nm in names_in:
            slug_base = slugify(nm)[:120] or "collection"
            if slug_base in existing_slugs and skip_existing:
                collisions.append(f"{nm} (slug '{slug_base}' exists)")
                continue
            if slug_base in existing_slugs and fail_existing:
                raise CommandError(f"Slug already exists: {slug_base!r} for {nm!r}")
            to_slugify.append(nm)

        if collisions:
            for c in collisions:
                self.stderr.write(self.style.WARNING(f"Skipping ({c})."))
            if not to_slugify:
                self.stdout.write(
                    self.style.WARNING("Nothing to create after filtering.")
                )
                return

        max_so = ClassCollection.objects.aggregate(m=Max("sort_order")).get(
            "m"
        )
        sort_base = max_so if max_so is not None else -1

        profiles: dict[str, dict[str, Any]] = {}
        if automated:
            if dry_run:
                for nm in to_slugify:
                    profiles[nm] = _heuristic_bundle(nm)
            elif no_gemini:
                for nm in to_slugify:
                    profiles[nm] = _heuristic_bundle(nm)
            else:
                batch = _gemini_bulk_profiles(
                    to_slugify,
                    curator_title_focus=(
                        preset_choice == PRESET_EXPERIENCE_THEMES_LABEL
                    ),
                )
                profiles = {p["name"]: p for p in batch}

        objs: list[ClassCollection] = []
        slug_reserved = set(reserved)

        for i, nm in enumerate(to_slugify):
            sort_order = sort_base + 1 + i
            slug = _unique_slug(slugify(nm)[:120] or "collection", slug_reserved)

            if automated:
                p = profiles.get(nm) or _heuristic_bundle(nm)
                rules = {"ai_criteria": p["ai_criteria"]}
                desc = p["description"]
                aliases = list(p["search_aliases"])
                objs.append(
                    ClassCollection(
                        name=nm[:100],
                        slug=slug,
                        description=(desc[:2000] if desc else ""),
                        type="automated",
                        automation_rules=rules,
                        is_active=True,
                        sort_order=sort_order,
                        search_aliases=aliases,
                        is_searchable=True,
                        show_in_i_want=iw,
                        show_in_featured_categories=False,
                        show_on_homepage_rows=True,
                        icon_name="",
                        color=(chip_color[:20] if chip_color else ""),
                        parent=None,
                    )
                )
            else:
                objs.append(
                    ClassCollection(
                        name=nm[:100],
                        slug=slug,
                        description=f"Explore {nm.lower()} classes and workshops.",
                        type="manual",
                        automation_rules={},
                        is_active=True,
                        sort_order=sort_order,
                        search_aliases=[nm.lower()],
                        is_searchable=True,
                        show_in_i_want=iw,
                        show_in_featured_categories=False,
                        show_on_homepage_rows=True,
                        icon_name="",
                        color=(chip_color[:20] if chip_color else ""),
                        parent=None,
                    )
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"[plan] {'automated' if automated else 'manual'} {nm[:100]} -> {slug} sort_order={sort_order}"
                )
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run - DB unchanged."))
            return

        classify_mode = None
        if automated and not skip_classify:
            if sync_classify:
                classify_mode = "sync"
            else:
                classify_mode = "celery"

        theme_coverage_ids_ordered: list[int] = []

        with transaction.atomic():
            ClassCollection.objects.bulk_create(objs)

            if run_theme_coverage and preset_choice == PRESET_EXPERIENCE_THEMES_LABEL:
                ids_resolved: list[int] = []
                for nm in PRESET_EXPERIENCE_THEME_NAMES:
                    display = str(nm).strip()[:100]
                    row = (
                        ClassCollection.objects.filter(
                            name=display,
                            type="automated",
                            parent__isnull=True,
                        )
                        .order_by("-id")
                        .first()
                    )
                    if row:
                        ids_resolved.append(row.pk)
                theme_coverage_ids_ordered = ids_resolved
                miss = len(PRESET_EXPERIENCE_THEME_NAMES) - len(ids_resolved)
                if miss > 0:
                    self.stderr.write(
                        self.style.WARNING(
                            f"Preset coverage: only {len(ids_resolved)} / "
                            f"{len(PRESET_EXPERIENCE_THEME_NAMES)} themed collections resolved by name "
                            "(check skipped slugs)."
                        )
                    )

            cov_ids_capture = list(theme_coverage_ids_ordered)

            def _after_commit(ids=cov_ids_capture):
                _schedule_revalidate()
                if classify_mode == "celery":
                    from celery import chain

                    from quickstart.tasks.business_tasks import (
                        ensure_preset_experience_theme_coverage_task,
                        update_trending_collections_task,
                    )

                    if (
                        run_theme_coverage
                        and preset_choice == PRESET_EXPERIENCE_THEMES_LABEL
                        and ids
                    ):
                        chain(
                            update_trending_collections_task.s(),
                            ensure_preset_experience_theme_coverage_task.si(ids),
                        ).delay()
                        self.stdout.write(
                            self.style.NOTICE(
                                "Queued chain: update_trending_collections_task "
                                "-> ensure_preset_experience_theme_coverage_task"
                            )
                        )
                    else:
                        from CEBackend.celery import app as celery_app

                        celery_app.send_task(
                            "quickstart.tasks.business_tasks.update_trending_collections_task"
                        )
                        self.stdout.write(
                            self.style.NOTICE(
                                "Queued quickstart.tasks.business_tasks.update_trending_collections_task "
                                "(Celery)"
                            )
                        )
                    return

                if (
                    classify_mode is None
                    and skip_classify
                    and run_theme_coverage
                    and preset_choice == PRESET_EXPERIENCE_THEMES_LABEL
                    and ids
                ):
                    from quickstart.tasks.business_tasks import (
                        ensure_preset_experience_theme_coverage_task,
                    )

                    ensure_preset_experience_theme_coverage_task.delay(ids)
                    self.stdout.write(
                        self.style.NOTICE(
                            "Queued ensure_preset_experience_theme_coverage_task "
                            "(preset coverage; classify skipped)"
                        )
                    )

            transaction.on_commit(_after_commit)
        self.stdout.write(
            self.style.SUCCESS(f"Inserted {len(objs)} collection(s).")
        )

        if classify_mode == "sync":
            from quickstart.tasks.business_tasks import (
                update_trending_collections_task,
            )

            self.stdout.write(
                self.style.NOTICE(
                    "Running update_trending_collections_task synchronously..."
                )
            )
            update_trending_collections_task.apply()
            if (
                run_theme_coverage
                and preset_choice == PRESET_EXPERIENCE_THEMES_LABEL
                and theme_coverage_ids_ordered
            ):
                self.stdout.write(
                    self.style.NOTICE(
                        "Running preset experience-theme coverage (sync)..."
                    )
                )
                stats = ensure_preset_collection_memberships(
                    theme_coverage_ids_ordered
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        "Preset coverage: "
                        f"already_had={stats.get('active_classes_already_had_preset', 0)} "
                        f"keyword_linked={stats.get('keyword_fallback_linked', 0)} "
                        f"defaulted={stats.get('default_fallback_linked', 0)}"
                    )
                )
