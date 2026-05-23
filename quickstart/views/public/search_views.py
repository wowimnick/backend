"""
Public search autocomplete: collections (ClassCollection) + classes (keyword FTS).
"""

import hashlib
import logging
import os

from django.core.cache import cache
from django.db.models import Count, F, Q
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from quickstart.models import ClassCollection
from quickstart.serializers.public.public_class_serializers import (
    PublicClassImageKeySerializer,
)
from quickstart.services.search_suggest_service import (
    match_collection_by_alias,
    suggest_class_instances,
)

logger = logging.getLogger(__name__)


class SearchSuggestAnonThrottle(AnonRateThrottle):
    # Autocomplete fires many requests while typing; keep above typical burst + navigation.
    rate = "120/minute"


class SearchSuggestUserThrottle(UserRateThrottle):
    rate = "300/minute"


def _normalize_query(q):
    return (q or "").strip()


def _cache_key_fragment(q_normalized: str, limit: int) -> str:
    digest = hashlib.sha256(q_normalized.lower().encode("utf-8")).hexdigest()[:24]
    return f"suggest:v1:{digest}:l{limit}"


def _collection_image_url(obj, request):
    if not obj.image or not obj.image.name:
        return None
    try:
        return request.build_absolute_uri(obj.image.url)
    except Exception:
        return None


def _serialize_collection(obj, request, count: int, matched_alias=None):
    row = {
        "slug": obj.slug,
        "name": obj.name,
        "image": _collection_image_url(obj, request),
        "count": count,
    }
    if matched_alias:
        row["matched_alias"] = matched_alias
    return row


def _serialize_class(klass, request):
    """Slim payload: cover medium_url + pricing."""
    images = getattr(klass, "images", None)
    image_list = list(images.all()) if images is not None else []
    thumb = None
    if image_list:
        preferred = sorted(
            image_list,
            key=lambda img: (not getattr(img, "isCover", False)),
        )[0]
        ser = PublicClassImageKeySerializer(preferred, context={"request": request})
        thumb = ser.data.get("medium_url")

    session_p = klass.min_session_price
    course_p = klass.min_course_price
    price_from = None
    if session_p is not None and course_p is not None:
        price_from = min(session_p, course_p)
    elif session_p is not None:
        price_from = session_p
    elif course_p is not None:
        price_from = course_p

    return {
        "id": klass.classId,
        "slug": klass.slug,
        "title": klass.title,
        "thumbnail": thumb,
        "business_name": (
            klass.businessId.businessName if klass.businessId_id else ""
        ),
        "price_from": price_from,
    }


def _suggest_collections(q_normalized: str, limit: int, request):
    """
    Short queries (1–2 chars): prefix match on name only.
    Longer: trigram rank on name + description when available; else icontains.
    """
    base = ClassCollection.objects.filter(is_active=True, is_searchable=True)
    short = len(q_normalized) <= 2

    if short:
        qs = (
            base.filter(name__istartswith=q_normalized)
            .annotate(
                class_count=Count(
                    "classes",
                    filter=Q(classes__status="active"),
                    distinct=True,
                )
            )
            .order_by("sort_order", "name")[:limit]
        )
        return list(qs)

    # Trigram when extension exists
    try:
        from django.contrib.postgres.search import TrigramSimilarity

        qs = (
            base.annotate(
                rn=TrigramSimilarity("name", q_normalized),
                rd=TrigramSimilarity("description", q_normalized),
                rank=F("rn") + 0.3 * F("rd"),
                class_count=Count(
                    "classes",
                    filter=Q(classes__status="active"),
                    distinct=True,
                ),
            )
            .filter(rank__gt=0.08)
            .order_by("-rank", "sort_order", "name")[:limit]
        )
        results = list(qs)
        if results:
            return results
    except Exception as e:
        logger.debug("Trigram collection search unavailable: %s", e)

    qs = (
        base.filter(
            Q(name__icontains=q_normalized) | Q(description__icontains=q_normalized)
        )
        .annotate(
            class_count=Count(
                "classes",
                filter=Q(classes__status="active"),
                distinct=True,
            )
        )
        .order_by("sort_order", "name")[:limit]
    )
    return list(qs)


class PublicCollectionPlacementListView(APIView):
    """
    Lightweight list of collections for homepage / explore pickers.
    ?placement=i_want | featured | all
    """

    permission_classes = [AllowAny]
    throttle_classes = [SearchSuggestAnonThrottle, SearchSuggestUserThrottle]

    def get(self, request):
        from quickstart.serializers.public.public_class_serializers import (
            PublicCollectionSerializer,
        )

        placement = (request.query_params.get("placement") or "i_want").lower()
        base = ClassCollection.objects.filter(is_active=True)
        if placement == "i_want":
            qs = base.filter(show_in_i_want=True).order_by("sort_order", "name")
        elif placement == "featured":
            qs = base.filter(show_in_featured_categories=True).order_by(
                "sort_order", "name"
            )
        else:
            qs = base.order_by("sort_order", "name")
        qs = qs.annotate(
            active_class_count=Count(
                "classes",
                filter=Q(classes__status="active"),
                distinct=True,
            )
        )
        ser = PublicCollectionSerializer(qs, many=True, context={"request": request})
        return Response(ser.data)


class SearchSuggestView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [SearchSuggestAnonThrottle, SearchSuggestUserThrottle]

    def get(self, request):
        raw_q = request.query_params.get("q", "")
        q_normalized = _normalize_query(raw_q)
        if not q_normalized:
            return Response(
                {"detail": "Query parameter 'q' is required and cannot be empty."},
                status=400,
            )

        try:
            limit = int(request.query_params.get("limit", "5"))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 10))

        lat = request.query_params.get("lat")
        lng = request.query_params.get("lng")
        include_coords = bool(lat and lng)

        def build_payload():
            cols = _suggest_collections(q_normalized, limit, request)
            alias_match = match_collection_by_alias(q_normalized)
            seen_slugs = set()
            col_data = []
            if alias_match:
                coll_ann = (
                    ClassCollection.objects.filter(pk=alias_match.pk)
                    .annotate(
                        class_count=Count(
                            "classes",
                            filter=Q(classes__status="active"),
                            distinct=True,
                        )
                    )
                    .first()
                )
                ac = int(getattr(coll_ann, "class_count", 0) or 0)
                col_data.append(
                    _serialize_collection(
                        coll_ann or alias_match,
                        request,
                        ac,
                        matched_alias=q_normalized,
                    )
                )
                seen_slugs.add(alias_match.slug)
            for c in cols:
                if c.slug in seen_slugs:
                    continue
                col_data.append(
                    _serialize_collection(
                        c, request, getattr(c, "class_count", 0) or 0
                    )
                )
                if len(col_data) >= limit:
                    break
            class_rows = suggest_class_instances(
                q_normalized, limit, lat_str=lat, lng_str=lng
            )
            class_data = [_serialize_class(k, request) for k in class_rows]
            return {"collections": col_data, "classes": class_data}

        if include_coords:
            payload = build_payload()
            return Response(payload)

        cache_key = _cache_key_fragment(q_normalized, limit)
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        payload = build_payload()
        cache.set(cache_key, payload, 60)
        return Response(payload)


LOCATION_PRESETS_CACHE_KEY = "public:location_presets:v1"
LOCATION_PRESETS_CACHE_SECONDS = 600


class LocationPresetsView(APIView):
    """
    Explore location shortcuts with at least one bookable class inside the
    census boundary polygon (no radius fallback).
    """

    permission_classes = [AllowAny]
    throttle_classes = [SearchSuggestAnonThrottle, SearchSuggestUserThrottle]

    def get(self, request):
        from django.utils import timezone

        from quickstart.services.location_preset_service import (
            get_location_presets_with_coverage,
            serialize_presets_for_api,
        )

        cached = cache.get(LOCATION_PRESETS_CACHE_KEY)
        if cached is not None:
            return Response(cached)

        presets = serialize_presets_for_api(get_location_presets_with_coverage())
        payload = {
            "generatedAt": timezone.now().isoformat(),
            "presets": presets,
        }
        cache.set(LOCATION_PRESETS_CACHE_KEY, payload, LOCATION_PRESETS_CACHE_SECONDS)
        return Response(payload)

