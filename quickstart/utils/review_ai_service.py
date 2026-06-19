"""
Gemini-powered selection of Google reviews to feature on the homepage hero.

Called once per build by the `refresh_homepage_reviews` management command
(run from entrypoint.sh on the web container). Gemini picks the most
compelling, diverse reviews from the imported Google reviews pool; each
selected review is linked to a representative class slug for deep-linking.
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
# Default number of reviews Gemini should pick for the hero strip.
DEFAULT_FEATURED_COUNT = 6
# Only consider reviews rated >= this for the hero.
MIN_RATING = 4
# Cap comment length sent to Gemini (keeps prompt small).
MAX_COMMENT_CHARS = 400


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

    # Precompute one representative class slug per business (most-reviewed class).
    business_ids = {r.business_id for r in reviews_qs if r.business_id is not None}
    representative_slug_by_business = {}
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
                representative_slug_by_business[biz_id] = cls.slug

    candidates = []
    for r in reviews_qs:
        slug = representative_slug_by_business.get(r.business_id)
        if not slug:
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
            "class_slug": slug,
        })
    return candidates


def _call_gemini(candidates, count):
    """
    Send candidate reviews to Gemini and ask it to pick the most compelling,
    diverse set for a homepage hero. Returns list of google_review_id strings,
    or None on failure.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        logger.error("GEMINI_API_KEY is missing; cannot select featured reviews.")
        return None

    client = genai.Client(api_key=api_key)

    # Build a compact JSON payload of candidates for the prompt.
    candidates_json = json.dumps(candidates, ensure_ascii=False)

    prompt = f"""You are the homepage editor for ClassEasily, a platform where people discover and book local experiences and classes.

Below is a JSON array of candidate Google reviews from our customers. Each review has a google_review_id, reviewer_name, rating, comment, review_date, business_name, and class_slug (a deep link to a class page on our site).

Pick the {count} most compelling reviews to feature on the homepage hero strip. Optimize for:
- Enthusiastic, specific, believable praise (avoid generic "great experience" filler).
- Diversity across businesses and review tones (variety of classes/experiences).
- Short quotes read well on a small chip (under ~140 chars is ideal; trim long comments in your reasoning but return the FULL original google_review_id).
- All selected reviews must be 4 or 5 stars.

Return a single JSON object only (no markdown fences, no commentary) with this exact shape:
{{"selected": ["<google_review_id>", "<google_review_id>", ...]}}

Order selected by how prominently it should appear (best first). Return exactly {count} ids (or fewer if the pool is smaller).

Candidate reviews:
{candidates_json}
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

    selected = data.get("selected") if isinstance(data, dict) else None
    if not isinstance(selected, list):
        logger.warning("Gemini review-selection response missing 'selected' list.")
        return None

    # Validate ids exist in the candidate pool (defend against hallucination).
    valid_ids = {c["google_review_id"] for c in candidates}
    selected_ids = [sid for sid in selected if isinstance(sid, str) and sid in valid_ids]
    if not selected_ids:
        logger.warning("Gemini returned no valid review ids; falling back.")
        return None

    return selected_ids[:count]


def select_featured_homepage_reviews(count=DEFAULT_FEATURED_COUNT):
    """
    High-level entry point: gather candidates, ask Gemini to pick, and upsert
    FeaturedHomepageReview rows for the current build id.

    Returns the number of featured reviews persisted, or 0 on failure.
    Never raises; callers (management command) rely on graceful failure.
    """
    from quickstart.utils.deploy_build_id import get_deploy_build_id

    try:
        candidates = _build_candidate_pool()
        if not candidates:
            logger.warning("No candidate Google reviews available to feature.")
            return 0
        if len(candidates) <= count:
            # Small pool — use them all without calling Gemini.
            selected_ids = [c["google_review_id"] for c in candidates]
        else:
            selected_ids = _call_gemini(candidates, count)
            if not selected_ids:
                logger.warning("Gemini review selection failed; using fallback ordering.")
                # Fallback: take the most recent `count` candidates.
                selected_ids = [c["google_review_id"] for c in candidates[:count]]

        # Build a lookup of candidate by id for fast upsert.
        by_id = {c["google_review_id"]: c for c in candidates}

        build_id = get_deploy_build_id() or ""

        with transaction.atomic():
            # Replace this build's set atomically.
            FeaturedHomepageReview.objects.filter(selection_build_id=build_id).delete()
            new_rows = []
            for order, gid in enumerate(selected_ids):
                c = by_id.get(gid)
                if not c:
                    continue
                new_rows.append(FeaturedHomepageReview(
                    google_review=c["review"],
                    class_slug=c["class_slug"],
                    business_name=c["business_name"],
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
