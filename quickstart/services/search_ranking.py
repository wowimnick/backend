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
W_NEWNESS = 0.4
QUALITY_SCORE_MAX_DESCRIPTION_LEN = 1000
QUALITY_SCORE_BASE_IMAGES = 5
QUALITY_SCORE_IDEAL_IMAGES = 10
RECENCY_HALFLIFE_DAYS = 180
REVIEW_COUNT_FOR_MAX_SCORE = 50


def compute_search_relevance_score(
    *,
    created_at: datetime,
    description_len: int,
    image_count: int,
    average_rating: Decimal | float,
    review_count: int,
    business_featured: bool,
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

    rc = float(review_count or 0)
    review_count_score = math.log10(rc + 1.0) / math.log10(
        REVIEW_COUNT_FOR_MAX_SCORE + 1.0
    )

    newness_score = math.pow(
        2.0, (-1.0 * days_old) / float(RECENCY_HALFLIFE_DAYS)
    )

    featured_multiplier = W_FEATURED if business_featured else 1.0

    relevance = (
        W_QUALITY * quality_score
        + W_RATING * rating_score
        + W_REVIEW_COUNT * review_count_score
        + W_NEWNESS * newness_score
    ) * featured_multiplier
    return float(relevance)
