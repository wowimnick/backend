"""Denormalized relevance score (matches legacy SQL formula in PublicClassViewSet)."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

W_FEATURED = 1.3
W_QUALITY = 0.8
W_RATING = 1.0
W_REVIEW_COUNT = 0.8
W_INSTAGRAM_FOLLOWERS = 0.8
W_NEWNESS = 0.4
QUALITY_SCORE_MAX_DESCRIPTION_LEN = 1000
QUALITY_SCORE_BASE_IMAGES = 5
QUALITY_SCORE_IDEAL_IMAGES = 10
RECENCY_HALFLIFE_DAYS = 180
REVIEW_COUNT_FOR_MAX_SCORE = 50
FOLLOWER_COUNT_FOR_MAX_SCORE = 10_000


def log_normalized_count_score(count: int, count_for_max: int) -> float:
    """Log-scaled 0..1 score; ``count_for_max`` maps to 1.0."""
    c = float(count or 0)
    return math.log10(c + 1.0) / math.log10(count_for_max + 1.0)


def compute_weighted_social_proof_score(
    *,
    review_count: int,
    instagram_follower_count: int | None = None,
) -> float:
    """Reviews and Instagram followers compete; only the stronger signal counts."""
    review_term = W_REVIEW_COUNT * log_normalized_count_score(
        review_count, REVIEW_COUNT_FOR_MAX_SCORE
    )
    follower_term = W_INSTAGRAM_FOLLOWERS * log_normalized_count_score(
        instagram_follower_count or 0, FOLLOWER_COUNT_FOR_MAX_SCORE
    )
    return max(review_term, follower_term)


def compute_search_relevance_score(
    *,
    created_at: datetime,
    description_len: int,
    image_count: int,
    average_rating: Decimal | float,
    review_count: int,
    business_featured: bool,
    instagram_follower_count: int | None = None,
) -> float:
    """Python mirror of _calculate_relevance_score annotation."""
    now = timezone.now()
    if timezone.is_naive(created_at):
        created_at = timezone.make_aware(created_at, timezone.utc)
    days_old = (now - created_at).total_seconds() / 86400.0

    image_score = 0.0
    if image_count >= QUALITY_SCORE_IDEAL_IMAGES:
        image_score = 1.0
    elif image_count > QUALITY_SCORE_BASE_IMAGES:
        num = math.log10(
            float(image_count) - float(QUALITY_SCORE_BASE_IMAGES) + 1.0
        )
        den = math.log10(
            float(QUALITY_SCORE_IDEAL_IMAGES - QUALITY_SCORE_BASE_IMAGES) + 1.0
        )
        image_score = num / den if den else 0.0

    desc_len = max(0, min(description_len, QUALITY_SCORE_MAX_DESCRIPTION_LEN))
    description_score = math.log10(desc_len + 1.0) / math.log10(
        QUALITY_SCORE_MAX_DESCRIPTION_LEN + 1.0
    )
    quality_score = (description_score + image_score) / 2.0

    ar = float(average_rating or 0)
    rating_score = ar / 5.0

    social_proof_score = compute_weighted_social_proof_score(
        review_count=review_count,
        instagram_follower_count=instagram_follower_count,
    )

    newness_score = math.pow(
        2.0, (-1.0 * days_old) / float(RECENCY_HALFLIFE_DAYS)
    )

    featured_multiplier = W_FEATURED if business_featured else 1.0

    relevance = (
        W_QUALITY * quality_score
        + W_RATING * rating_score
        + social_proof_score
        + W_NEWNESS * newness_score
    ) * featured_multiplier
    return float(relevance)
