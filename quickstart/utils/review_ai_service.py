"""
Gemini-powered selection of Google reviews to feature on the homepage hero.

Called once per build by the `refresh_homepage_reviews` management command
(run from entrypoint.sh on the web container). Gemini picks two polished
reviews from each of at least three classes; each selected review is linked
to a representative class slug for deep-linking.
"""
import json
import logging

from django.conf import settings
from django.db import transaction

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from quickstart.models import (
    ClassesMain,
    FeaturedHomepageReview,
    ImportedGoogleReview,
)

logger = logging.getLogger(__name__)

# How many candidate reviews to send to Gemini to choose from.
CANDIDATE_POOL_SIZE = 200
# Featured hero: at least this many distinct classes, two reviews each.
MIN_FEATURED_CLASSES = 3
REVIEWS_PER_CLASS = 5
DEFAULT_FEATURED_COUNT = MIN_FEATURED_CLASSES * REVIEWS_PER_CLASS
# Only consider reviews rated >= this for the hero.
MIN_RATING = 4
# Cap raw comment length sent to Gemini (keeps prompt small).
MAX_COMMENT_CHARS = 400
# Polished quote length for homepage chips.
MAX_DISPLAY_COMMENT_CHARS = 160


def _candidate_models():
    """Return ordered list of Gemini model names to try (same pattern as blog_ai_service)."""
    configured_models = getattr(settings, "GEMINI_REVIEW_MODELS", None)
    if isinstance(configured_models, str) and configured_models.strip():
        models = [m.strip() for m in configured_models.split(",") if m.strip()]
        if models:
            return models

    single_model = (
        getattr(settings, "GEMINI_REVIEW_MODEL", None)
        or getattr(settings, "GEMINI_MODEL", None)
    )
    if isinstance(single_model, str) and single_model.strip():
        return [single_model.strip()]

    return ["gemini-2.5-flash", "gemini-2.0-flash"]


def _build_candidate_pool(count=CANDIDATE_POOL_SIZE):
    """
    Gather candidate Google reviews across all businesses, each annotated with
    a representative class slug for its business (highest-rated class).

    Returns a list of dicts:
        {
            "review": <ImportedGoogleReview instance>,
            "google_review_id", "reviewer_name", "rating", "comment",
            "review_date" (ISO), "business_id", "business_name", "class_slug",
        }
    Reviews with no linkable class are skipped (cannot deep-link from hero).
    """
    reviews_qs = list(
        ImportedGoogleReview.objects
        .filter(rating__gte=MIN_RATING)
        .exclude(comment__isnull=True)
        .exclude(comment="")
        .select_related("business")
        .order_by("-review_date", "-created_at")[:count]
    )

    # Precompute one representative class per business (most-reviewed class).
    business_ids = {r.business_id for r in reviews_qs if r.business_id is not None}
    representative_class_by_business = {}
    if business_ids:
        for biz_id in business_ids:
            cls = (
                ClassesMain.objects
                .filter(businessId_id=biz_id, slug__isnull=False)
                .exclude(slug="")
                .order_by("-platform_review_count", "classId")
                .first()
            )
            if cls:
                representative_class_by_business[biz_id] = cls

    candidates = []
    for r in reviews_qs:
        cls = representative_class_by_business.get(r.business_id)
        if not cls:
            continue  # no deep-link target — skip
        comment = (r.comment or "").strip()
        if len(comment) > MAX_COMMENT_CHARS:
            comment = comment[:MAX_COMMENT_CHARS].rstrip() + "\u2026"
        candidates.append({
            "review": r,
            "google_review_id": r.google_review_id,
            "reviewer_name": r.reviewer_name,
            "rating": r.rating,
            "comment": comment,
            "review_date": r.review_date.isoformat() if r.review_date else None,
            "business_name": r.business.businessName if r.business_id else "",
            "class_slug": cls.slug,
            "class_title": cls.title,
        })
    return candidates


def _group_candidates_by_class(candidates, min_reviews_per_class=REVIEWS_PER_CLASS):
    """Group candidates by class_slug; keep classes with enough reviews to pick from."""
    grouped = {}
    for candidate in candidates:
        slug = candidate["class_slug"]
        grouped.setdefault(slug, []).append(candidate)
    return {
        slug: class_candidates
        for slug, class_candidates in grouped.items()
        if len(class_candidates) >= min_reviews_per_class
    }


def _candidate_review_payload(candidate):
    return {
        "google_review_id": candidate["google_review_id"],
        "reviewer_name": candidate["reviewer_name"],
        "rating": candidate["rating"],
        "comment": candidate["comment"],
        "review_date": candidate["review_date"],
    }


def _class_prompt_payload(grouped_candidates):
    """JSON-serializable class-grouped payload for the Gemini prompt."""
    class_items = []
    for slug, class_candidates in sorted(
        grouped_candidates.items(),
        key=lambda item: (-len(item[1]), item[0]),
    ):
        sample = class_candidates[0]
        class_items.append({
            "class_slug": slug,
            "class_title": sample.get("class_title") or "",
            "business_name": sample.get("business_name") or "",
            "reviews": [_candidate_review_payload(c) for c in class_candidates],
        })
    return class_items


def _call_gemini(grouped_candidates, class_count, reviews_per_class):
    """
    Ask Gemini to pick reviews per class and return polished display quotes.

    Returns a list of dicts:
        {"google_review_id": str, "display_comment": str, "class_slug": str}
    or None on failure.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        logger.error("GEMINI_API_KEY is missing; cannot select featured reviews.")
        return None

    client = genai.Client(api_key=api_key)
    total_reviews = class_count * reviews_per_class

    classes_json = json.dumps(
        _class_prompt_payload(grouped_candidates),
        ensure_ascii=False,
    )

    prompt = f"""You are the homepage editor for ClassEasily, a platform where people discover and book local experiences and classes.

Below is a JSON array of classes. Each class has a class_slug, class_title, business_name, and a reviews array. Every review includes google_review_id, reviewer_name, rating, comment, and review_date. Reviews are real Google reviews from customers of that business; the class_slug is the page we link to on our site.

Your task:
1. Choose exactly {class_count} different classes (minimum {MIN_FEATURED_CLASSES} when the pool allows).
2. For each chosen class, choose exactly {reviews_per_class} reviews from that class's reviews array.
3. For every chosen review, write a display_comment: a polished, professional, positive quote suitable for a homepage hero chip. You may lightly rewrite awkward grammar, remove slang, and tighten wording, but keep the meaning faithful to the original praise. Do not invent facts, experiences, or ratings that are not supported by the source review.
4. display_comment must sound natural, warm, and credible — not marketing fluff. Prefer specific praise over generic lines like "great experience".
5. display_comment must be at most {MAX_DISPLAY_COMMENT_CHARS} characters.
6. Only choose reviews rated 4 or 5 stars.

Return a single JSON object only (no markdown fences, no commentary) with this exact shape:
{{
  "classes": [
    {{
      "class_slug": "<class_slug>",
      "reviews": [
        {{
          "google_review_id": "<google_review_id>",
          "display_comment": "<polished quote>"
        }}
      ]
    }}
  ]
}}

Rules:
- Include exactly {reviews_per_class} reviews per class and {class_count} classes ({total_reviews} reviews total), unless the candidate data truly cannot support that.
- Every google_review_id must come from the matching class's reviews array in the input.
- Do not repeat the same google_review_id.
- Order classes and reviews by how prominently they should appear on the homepage (best first).

Candidate classes:
{classes_json}
"""

    response = None
    candidate_models = _candidate_models()
    for idx, model_name in enumerate(candidate_models, start=1):
        try:
            logger.info(
                "Calling Gemini review-selector model %s (%s/%s)",
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
            logger.warning(
                "Gemini review-selector model %s failed with code=%s; retryable=%s; error=%s",
                model_name,
                status_code,
                is_retryable,
                model_error,
            )
            if not is_retryable or idx == len(candidate_models):
                raise

    if response is None:
        logger.warning("Gemini returned no response object for review selection.")
        return None

    if not response.text:
        logger.warning("Gemini returned empty response for review selection.")
        return None

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse Gemini review-selection JSON: %s", e)
        return None

    return _parse_gemini_selection(data, grouped_candidates, class_count, reviews_per_class)


def _trim_display_comment(text):
    comment = (text or "").strip()
    if len(comment) <= MAX_DISPLAY_COMMENT_CHARS:
        return comment
    return comment[: MAX_DISPLAY_COMMENT_CHARS - 1].rstrip() + "\u2026"


def _parse_gemini_selection(data, grouped_candidates, class_count, reviews_per_class):
    """Validate Gemini JSON and flatten to ordered selection rows."""
    classes = data.get("classes") if isinstance(data, dict) else None
    if not isinstance(classes, list):
        logger.warning("Gemini review-selection response missing 'classes' list.")
        return None

    valid_ids_by_class = {
        slug: {c["google_review_id"] for c in class_candidates}
        for slug, class_candidates in grouped_candidates.items()
    }
    selected = []
    seen_ids = set()

    for class_entry in classes:
        if not isinstance(class_entry, dict):
            continue
        class_slug = class_entry.get("class_slug")
        if not isinstance(class_slug, str) or class_slug not in valid_ids_by_class:
            continue
        reviews = class_entry.get("reviews")
        if not isinstance(reviews, list):
            continue

        class_picks = []
        for review_entry in reviews:
            if not isinstance(review_entry, dict):
                continue
            review_id = review_entry.get("google_review_id")
            display_comment = review_entry.get("display_comment")
            if (
                not isinstance(review_id, str)
                or review_id not in valid_ids_by_class[class_slug]
                or review_id in seen_ids
                or not isinstance(display_comment, str)
                or not display_comment.strip()
            ):
                continue
            seen_ids.add(review_id)
            class_picks.append({
                "google_review_id": review_id,
                "display_comment": _trim_display_comment(display_comment),
                "class_slug": class_slug,
            })
            if len(class_picks) >= reviews_per_class:
                break

        if len(class_picks) == reviews_per_class:
            selected.extend(class_picks)

        if len(selected) >= class_count * reviews_per_class:
            break

    if len(selected) < min(class_count, len(grouped_candidates)) * reviews_per_class:
        logger.warning("Gemini returned an incomplete class-balanced selection.")
        return None

    return selected[: class_count * reviews_per_class]


def _fallback_selection(grouped_candidates, class_count, reviews_per_class):
    """Deterministic fallback: top classes by pool size, newest reviews per class."""
    ordered_classes = sorted(
        grouped_candidates.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )[:class_count]

    selected = []
    for class_slug, class_candidates in ordered_classes:
        picks = sorted(
            class_candidates,
            key=lambda c: c.get("review_date") or "",
            reverse=True,
        )[:reviews_per_class]
        for candidate in picks:
            selected.append({
                "google_review_id": candidate["google_review_id"],
                "display_comment": _trim_display_comment(candidate["comment"]),
                "class_slug": class_slug,
            })
    return selected


def select_featured_homepage_reviews(count=DEFAULT_FEATURED_COUNT):
    """
    High-level entry point: gather candidates, ask Gemini to pick, and upsert
    FeaturedHomepageReview rows for the current build id.

    Selection is class-balanced: by default 3 classes with 2 reviews each.
    Gemini may lightly rewrite quotes into display_comment for the homepage.

    Returns the number of featured reviews persisted, or 0 on failure.
    Never raises; callers (management command) rely on graceful failure.
    """
    from quickstart.utils.deploy_build_id import get_deploy_build_id

    try:
        reviews_per_class = REVIEWS_PER_CLASS
        class_count = max(MIN_FEATURED_CLASSES, count // reviews_per_class)

        candidates = _build_candidate_pool()
        if not candidates:
            logger.warning("No candidate Google reviews available to feature.")
            return 0

        grouped_candidates = _group_candidates_by_class(candidates)
        if not grouped_candidates:
            logger.warning(
                "No classes have at least %s candidate reviews for homepage featuring.",
                reviews_per_class,
            )
            return 0

        available_class_count = min(class_count, len(grouped_candidates))
        if available_class_count < MIN_FEATURED_CLASSES:
            logger.warning(
                "Only %s classes have enough reviews; need at least %s.",
                available_class_count,
                MIN_FEATURED_CLASSES,
            )

        selected_rows = _call_gemini(
            grouped_candidates,
            class_count=available_class_count,
            reviews_per_class=reviews_per_class,
        )
        if not selected_rows:
            logger.warning("Gemini review selection failed; using fallback ordering.")
            selected_rows = _fallback_selection(
                grouped_candidates,
                class_count=available_class_count,
                reviews_per_class=reviews_per_class,
            )

        if not selected_rows:
            return 0

        by_id = {c["google_review_id"]: c for c in candidates}
        build_id = get_deploy_build_id() or ""

        with transaction.atomic():
            FeaturedHomepageReview.objects.filter(selection_build_id=build_id).delete()
            new_rows = []
            for order, row in enumerate(selected_rows):
                candidate = by_id.get(row["google_review_id"])
                if not candidate:
                    continue
                new_rows.append(FeaturedHomepageReview(
                    google_review=candidate["review"],
                    class_slug=row.get("class_slug") or candidate["class_slug"],
                    business_name=candidate["business_name"],
                    display_comment=row.get("display_comment") or "",
                    display_order=order,
                    selection_build_id=build_id,
                ))
            if new_rows:
                FeaturedHomepageReview.objects.bulk_create(new_rows)
            logger.info(
                "Persisted %s featured homepage reviews for build '%s'.",
                len(new_rows),
                build_id,
            )
            return len(new_rows)

    except Exception as e:
        logger.error("select_featured_homepage_reviews failed: %s", e, exc_info=True)
        return 0
