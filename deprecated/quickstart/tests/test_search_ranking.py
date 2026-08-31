from datetime import datetime, timezone as dt_timezone

import pytest

from quickstart.services.search_ranking import (
    FOLLOWER_COUNT_FOR_MAX_SCORE,
    REVIEW_COUNT_FOR_MAX_SCORE,
    W_INSTAGRAM_FOLLOWERS,
    W_REVIEW_COUNT,
    compute_search_relevance_score,
    compute_weighted_social_proof_score,
    log_normalized_count_score,
)


def _base_kwargs(**overrides):
    kwargs = {
        "created_at": datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
        "description_len": 500,
        "image_count": 10,
        "average_rating": 4.5,
        "review_count": 0,
        "business_featured": False,
        "instagram_follower_count": None,
    }
    kwargs.update(overrides)
    return kwargs


class TestLogNormalizedCountScore:
    def test_zero_count_is_zero(self):
        assert log_normalized_count_score(0, REVIEW_COUNT_FOR_MAX_SCORE) == 0.0

    def test_max_count_is_one(self):
        assert log_normalized_count_score(
            REVIEW_COUNT_FOR_MAX_SCORE, REVIEW_COUNT_FOR_MAX_SCORE
        ) == pytest.approx(1.0)


class TestWeightedSocialProofScore:
    def test_reviews_and_followers_do_not_stack(self):
        reviews_only = compute_weighted_social_proof_score(
            review_count=50, instagram_follower_count=0
        )
        both = compute_weighted_social_proof_score(
            review_count=50, instagram_follower_count=50_000
        )
        assert both == pytest.approx(reviews_only)

    def test_followers_can_outrank_low_review_counts(self):
        review_term = compute_weighted_social_proof_score(
            review_count=5, instagram_follower_count=0
        )
        follower_term = compute_weighted_social_proof_score(
            review_count=0, instagram_follower_count=5_000
        )
        assert follower_term > review_term

    def test_review_weight_matches_follower_weight_at_max(self):
        review_max = W_REVIEW_COUNT * log_normalized_count_score(
            REVIEW_COUNT_FOR_MAX_SCORE, REVIEW_COUNT_FOR_MAX_SCORE
        )
        follower_max = W_INSTAGRAM_FOLLOWERS * log_normalized_count_score(
            FOLLOWER_COUNT_FOR_MAX_SCORE, FOLLOWER_COUNT_FOR_MAX_SCORE
        )
        assert review_max == pytest.approx(follower_max)


class TestComputeSearchRelevanceScore:
    def test_instagram_followers_increase_default_relevance(self):
        without = compute_search_relevance_score(**_base_kwargs())
        with_followers = compute_search_relevance_score(
            **_base_kwargs(instagram_follower_count=8_000)
        )
        assert with_followers > without

    def test_strong_reviews_beat_moderate_followers(self):
        review_heavy = compute_search_relevance_score(
            **_base_kwargs(review_count=40, instagram_follower_count=500)
        )
        follower_heavy = compute_search_relevance_score(
            **_base_kwargs(review_count=0, instagram_follower_count=2_000)
        )
        assert review_heavy > follower_heavy
