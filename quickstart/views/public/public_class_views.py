import threading
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from django.db.models import (
    Q,
    Count,
    Min,
    Value,
    F,
    Subquery,
    OuterRef,
    DecimalField,
    IntegerField,
    Sum,
    Case,
    When,
    ExpressionWrapper,
    FloatField,
    Prefetch,
    Exists,
    CharField,
)
from django.db.models.functions import (
    Coalesce,
    Cast,
    StrIndex,
    Substr,
    ATan2,
    Greatest,
    Least,
    Extract,
    Length,
    Log,
    Now,
    Power,
)
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.utils import timezone
from django.core.cache import cache
from decimal import Decimal, InvalidOperation
import logging
from urllib.parse import quote
from datetime import (
    datetime,
    date,
    time,
    timedelta,
)
import copy
from uuid import UUID

from quickstart.models import (
    ClassCollection,
    ClassImage,
    ClassesMain,
    ClassOption,
    GeographicBoundary,
    ImportedGoogleReview,
    Reviews,
    Booking,
    Schedule,
    ScheduleInstance,
)
from quickstart.serializers import (
    PublicClassSerializer,
    ScheduleSerializer,
    PublicClassDetailSerializer,
    PublicReviewSerializer,
    ImportedGoogleReviewSerializer,
    PublicCollectionSerializer,
    HomepageClassSerializer
)
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.conf import settings

from quickstart.services.search_filter_params import normalize_booking_type_query
from quickstart.services.search_ranking import (
    FOLLOWER_COUNT_FOR_MAX_SCORE,
    QUALITY_SCORE_BASE_IMAGES,
    QUALITY_SCORE_IDEAL_IMAGES,
    QUALITY_SCORE_MAX_DESCRIPTION_LEN,
    RECENCY_HALFLIFE_DAYS,
    REVIEW_COUNT_FOR_MAX_SCORE,
    W_FEATURED,
    W_INSTAGRAM_FOLLOWERS,
    W_NEWNESS,
    W_QUALITY,
    W_RATING,
    W_REVIEW_COUNT,
)
from quickstart.services.search_suggest_service import match_collection_by_alias
from quickstart.services.search_geo_params import (
    CANADIAN_PROVINCES,
    DEFAULT_SEARCH_RADIUS_KM,
    business_states_for_province_field,
    normalize_province_name,
    is_toronto_gta_search_name as _is_toronto_gta_search_name,
    toronto_gta_center_point as _toronto_gta_center_point,
    toronto_gta_radius_km as _toronto_gta_radius_km,
)

logger = logging.getLogger(__name__)


def annotate_collection_active_class_count(queryset):
    """Distinct active classes linked to each collection (M2M)."""
    return queryset.annotate(
        active_class_count=Count(
            "classes",
            filter=Q(classes__status="active"),
            distinct=True,
        )
    )


def _public_class_options_prefetch_queryset():
    """
    Class options as exposed on public list/detail (PublicClassOptionSerializer).
    Restricting columns reduces prefetch payload vs. SELECT * on class_options
    (JSON and unused policy fields are heavy at scale).
    """
    return ClassOption.objects.only(
        "optionId",
        "classId",
        "title",
        "description",
        "booking_type",
        "level",
        "equipment",
        "tags",
        "cancellationPolicy",
        "cancellationCustomHours",
        "cancellationRefundPercentage",
        "price_type",
    ).order_by("optionId")


# Scope cache keys by environment so staging and prod share Redis without clearing each other's cache
_CACHE_ENV = getattr(settings, "DJANGO_ENV", "local")

PRESET_CACHE_PREFIX = "public_class_search_preset"
# Version key per env so staging/prod can share Redis without clearing each other
PRESET_CACHE_VERSION_KEY = f"public_class_search_preset_version:{_CACHE_ENV}"
# TTL so old keys expire and Redis does not grow unbounded (OOM). We invalidate selectively on content change.
PRESET_CACHE_TTL = 7 * 24 * 60 * 60  # 1 week
_prewarm_version_local = threading.local()

# Collection-only search cache (no location/category/keyword); same version invalidation as preset
COLLECTION_CACHE_PREFIX = "public_class_search_collection"
# Preset location + collection (e.g. Toronto + trending); same version invalidation
PRESET_COLLECTION_CACHE_PREFIX = "public_class_search_preset_collection"
PRESET_PREWARM_PAGE_SIZE = 50
# Collections list (homepage_content mode=collections); per-env so staging/prod don't overwrite
HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY = f"homepage_content_collections:{_CACHE_ENV}"
HOMEPAGE_CONTENT_COLLECTIONS_CACHE_TIMEOUT = 60 * 60  # 1 hour (only used until next collection change)
HOMEPAGE_SECTIONS_CACHE_KEY = f"homepage_sections_v1:{_CACHE_ENV}"
HOMEPAGE_SECTIONS_CACHE_TIMEOUT = 300  # 5 min (trending / date_night / next_week rows)


def _safe_cache_set(key, value, timeout=None):
    """Set cache key; on failure (e.g. Redis OOM) log and continue so the request still returns the response."""
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as e:
        logger.warning("Cache set failed (e.g. Redis OOM): key=%s, error=%s", key, e)


def set_prewarm_cache_version(version):
    """Used by prewarm task so cache keys are written with the new version before it goes live."""
    _prewarm_version_local.version = version


def clear_prewarm_cache_version():
    """Clear thread-local after prewarm run."""
    if hasattr(_prewarm_version_local, "version"):
        delattr(_prewarm_version_local, "version")


def _get_preset_search_cache_version():
    """Current cache version for preset search. Prewarm can set version via thread-local before bumping live."""
    if hasattr(_prewarm_version_local, "version"):
        return _prewarm_version_local.version
    return cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0


def invalidate_public_class_search_preset_cache(
    affected_locations=None,
    affected_collection_slugs=None,
):
    """Legacy Redis preset search cache removed; Typesense index tasks handle freshness."""
    logger.debug(
        "invalidate_public_class_search_preset_cache no-op (affected_locations=%s, affected_collection_slugs=%s)",
        affected_locations,
        affected_collection_slugs,
    )


def _is_count_only_request(request):
    """Lightweight search preview: same filters, response is only {"count": int}."""
    v = request.query_params.get("count_only")
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def _attach_resolved_collection_to_search_payload(data, meta):
    """Add resolved_collection to paginated search JSON when keyword matched a collection."""
    if not meta or not isinstance(data, dict):
        return data
    out = copy.deepcopy(data)
    out["resolved_collection"] = meta
    return out


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 100


class PublicClassViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a public list and detail view for active, verified classes.
    """

    serializer_class = PublicClassSerializer
    permission_classes = [AllowAny]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "title",
        "description",
        "businessId__businessName",
    ]
    ordering_fields = [
        "createdAt",
        "average_rating",
        "total_reviews",
        "relevance_score",
        "rank",
    ]
    ordering = ["-createdAt"]
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicClassDetailSerializer
        return super().get_serializer_class()

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        identifier = self.kwargs.get(self.lookup_field)
        if identifier.isdigit():
            filter_kwargs = {"pk": identifier}
            obj = get_object_or_404(queryset, **filter_kwargs)
        else:
            from quickstart.utils.class_slug_utils import resolve_class_by_slug

            try:
                obj = resolve_class_by_slug(queryset, identifier)
            except ClassesMain.DoesNotExist:
                raise Http404
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        request = self.request
        if request.user.is_authenticated:
            from quickstart.models import Favorites

            ctx["favorited_ids"] = set(
                Favorites.objects.filter(userId=request.user).values_list(
                    "classId", flat=True
                )
            )
        return ctx

    MIN_SESSION_PRICE_SUBQUERY = Subquery(
        Schedule.objects.filter(
            option__classId=OuterRef("pk"),
            option__booking_type="Single Session",
            date__gte=timezone.now().date(),
        )
        .order_by("price")
        .values("price")[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    MIN_COURSE_PRICE_SUBQUERY = Subquery(
        Schedule.objects.filter(
            option__classId=OuterRef("pk"),
            option__booking_type="Full Course",
            end_date__gte=timezone.now().date(),
        )
        .order_by("price")
        .values("price")[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )

    def get_queryset(self):
        # Platform + Google review rollups are denormalized on ClassesMain / BusinessInfo
        # (see platform_review_count, businessId__google_review_count) to avoid per-row subqueries.

        # Price subqueries (schedules still require scoped lookups)
        min_session_price_subquery = Subquery(
            Schedule.objects.filter(
                option__classId=OuterRef("pk"),
                option__booking_type="Single Session",
                date__gte=timezone.now().date(),
            )
            .order_by("price")
            .values("price")[:1],
            output_field=DecimalField(max_digits=10, decimal_places=2),
        )
        min_course_price_subquery = Subquery(
            Schedule.objects.filter(
                option__classId=OuterRef("pk"),
                option__booking_type="Full Course",
                end_date__gte=timezone.now().date(),
            )
            .order_by("price")
            .values("price")[:1],
            output_field=DecimalField(max_digits=10, decimal_places=2),
        )

        options_qs = _public_class_options_prefetch_queryset()
        if self.action == "retrieve":
            # Prefetch instances annotated with total_booked so PublicScheduleSerializer
            # can derive available_spots = max_participants - total_booked without
            # extra per-instance DB queries (N+1 prevention).
            instances_qs = ScheduleInstance.objects.filter(
                date__gte=timezone.now().date(),
                status="scheduled",
            ).annotate(
                total_booked=Coalesce(
                    Sum(
                        "bookings__participants",
                        filter=Q(bookings__status__in=["confirmed", "pending"]),
                    ),
                    Value(0),
                    output_field=IntegerField(),
                )
            )
            schedules_qs = Schedule.objects.filter(
                Q(date__gte=timezone.now().date())
                | Q(end_date__gte=timezone.now().date())
            ).prefetch_related(
                Prefetch(
                    "instances",
                    queryset=instances_qs,
                    to_attr="_prefetched_instances",
                )
            ).order_by("date", "time")
            options_qs = options_qs.prefetch_related(
                Prefetch("schedules", queryset=schedules_qs)
            )

        queryset = (
            ClassesMain.objects.select_related("businessId", "location_ref")
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=ClassImage.objects.only(
                        "imageId", "classId_id", "image", "isCover", "createdAt"
                    ).order_by("-isCover", "createdAt"),
                ),
                Prefetch("options", queryset=options_qs),
            )
            .filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
            )
            # Annotate raw counts/ratings from denormalized columns
            .annotate(
                p_rating_raw=Coalesce(
                    F("platform_avg_rating"), Value(Decimal("0.00"))
                ),
                p_count_raw=Coalesce(F("platform_review_count"), Value(0)),
                g_rating_raw=Coalesce(
                    F("businessId__google_avg_rating"),
                    Value(Decimal("0.00")),
                ),
                g_count_raw=Coalesce(
                    F("businessId__google_review_count"), Value(0)
                ),
            )
            # 5. Calculate Combined Totals (Used for Ranking)
            .annotate(
                # Total Reviews = Platform + Google
                review_count=ExpressionWrapper(
                    F("p_count_raw") + F("g_count_raw"),
                    output_field=IntegerField()
                ),
                
                # Calculate Weighted Sum: (P_Rating * P_Count) + (G_Rating * G_Count)
                weighted_sum=ExpressionWrapper(
                    (F("p_rating_raw") * F("p_count_raw")) + (F("g_rating_raw") * F("g_count_raw")),
                    output_field=DecimalField(max_digits=10, decimal_places=2)
                ),
            )
            # 6. Calculate Final Average Rating
            .annotate(
                average_rating=Case(
                    When(review_count=0, then=Value(Decimal("0.0"))),
                    default=ExpressionWrapper(
                        F("weighted_sum") / F("review_count"),
                        output_field=DecimalField(max_digits=3, decimal_places=1)
                    ),
                    output_field=DecimalField(max_digits=3, decimal_places=1)
                ),
                # Existing Price fields
                min_session_price=Coalesce(min_session_price_subquery, None),
                min_course_price=Coalesce(min_course_price_subquery, None),
                image_count=Count("images", distinct=True),
            )
        )

        # 7. Specific Logic for Retrieve Action (Detail View)
        if self.action == "retrieve":
            logger.debug("Action is 'retrieve', prefetching collections for class detail.")
            queryset = queryset.prefetch_related("collections")
            # Shortest schedule duration (minutes). Must call .order_by() with no args first
            # to clear Schedule.Meta.ordering ("day", "time"); otherwise PostgreSQL raises
            # GroupingError on the scalar subquery (ORDER BY day conflicts with aggregate).
            listing_duration_sq = Subquery(
                Schedule.objects.filter(option__classId=OuterRef("pk"))
                .order_by()
                .order_by("duration")
                .values("duration")[:1],
                output_field=IntegerField(null=True),
            )
            queryset = queryset.annotate(listing_duration_minutes=listing_duration_sq)

        return queryset.distinct()

    def _calculate_relevance_score(self, queryset):
        """
        Live relevance (same formula as Typesense denorm / search_ranking.compute_search_relevance_score).

        Homepage rows still use this so trending reflects current ratings and listing quality.
        ``search_relevance_score`` on ClassesMain is updated only when the search index runs.
        """
        days_old = ExpressionWrapper(
            Extract(Now() - F("createdAt"), "epoch")
            / Cast(Value(86400.0), FloatField()),
            output_field=FloatField(),
        )

        image_score_numerator = Log(
            Cast(Value(10), FloatField()),
            Cast(F("image_count"), FloatField())
            - Cast(Value(QUALITY_SCORE_BASE_IMAGES), FloatField())
            + Cast(Value(1), FloatField()),
        )
        image_score_denominator = Log(
            Cast(Value(10), FloatField()),
            Cast(
                Value(QUALITY_SCORE_IDEAL_IMAGES - QUALITY_SCORE_BASE_IMAGES),
                FloatField(),
            )
            + Cast(Value(1), FloatField()),
        )

        image_score = Case(
            When(
                image_count__gte=QUALITY_SCORE_IDEAL_IMAGES,
                then=Cast(Value(1.0), FloatField()),
            ),
            When(
                image_count__gt=QUALITY_SCORE_BASE_IMAGES,
                then=ExpressionWrapper(
                    image_score_numerator / image_score_denominator,
                    output_field=FloatField(),
                ),
            ),
            default=Cast(Value(0.0), FloatField()),
            output_field=FloatField(),
        )

        description_score = ExpressionWrapper(
            Log(
                Cast(Value(10), FloatField()),
                Length("description") + Cast(Value(1), FloatField()),
            )
            / Log(
                Cast(Value(10), FloatField()),
                Cast(Value(QUALITY_SCORE_MAX_DESCRIPTION_LEN + 1), FloatField()),
            ),
            output_field=FloatField(),
        )

        quality_score = ExpressionWrapper(
            (description_score + image_score) / Cast(Value(2.0), FloatField()),
            output_field=FloatField(),
        )

        rating_score = ExpressionWrapper(
            F("average_rating") / Cast(Value(5.0), FloatField()),
            output_field=FloatField(),
        )

        review_count_score = ExpressionWrapper(
            Log(
                Cast(Value(10), FloatField()),
                Cast(F("review_count"), FloatField()) + Cast(Value(1), FloatField()),
            )
            / Log(
                Cast(Value(10), FloatField()),
                Cast(Value(REVIEW_COUNT_FOR_MAX_SCORE + 1), FloatField()),
            ),
            output_field=FloatField(),
        )

        follower_count_score = ExpressionWrapper(
            Log(
                Cast(Value(10), FloatField()),
                Cast(
                    Coalesce(F("businessId__instagram_follower_count"), Value(0)),
                    FloatField(),
                )
                + Cast(Value(1), FloatField()),
            )
            / Log(
                Cast(Value(10), FloatField()),
                Cast(Value(FOLLOWER_COUNT_FOR_MAX_SCORE + 1), FloatField()),
            ),
            output_field=FloatField(),
        )

        review_social_term = ExpressionWrapper(
            Cast(Value(W_REVIEW_COUNT), FloatField()) * review_count_score,
            output_field=FloatField(),
        )
        follower_social_term = ExpressionWrapper(
            Cast(Value(W_INSTAGRAM_FOLLOWERS), FloatField()) * follower_count_score,
            output_field=FloatField(),
        )
        social_proof_score = Greatest(review_social_term, follower_social_term)

        newness_score = ExpressionWrapper(
            Power(
                Cast(Value(2), FloatField()),
                Cast(Value(-1), FloatField())
                * days_old
                / Cast(Value(RECENCY_HALFLIFE_DAYS), FloatField()),
            ),
            output_field=FloatField(),
        )

        featured_multiplier = Case(
            When(businessId__featured=True, then=Cast(Value(W_FEATURED), FloatField())),
            default=Cast(Value(1.0), FloatField()),
            output_field=FloatField(),
        )

        relevance_score = ExpressionWrapper(
            (
                (Cast(Value(W_QUALITY), FloatField()) * quality_score)
                + (Cast(Value(W_RATING), FloatField()) * rating_score)
                + social_proof_score
                + (Cast(Value(W_NEWNESS), FloatField()) * newness_score)
            )
            * featured_multiplier,
            output_field=FloatField(),
        )

        return queryset.annotate(relevance_score=relevance_score)

    def collection_children(self, request, parent_slug=None):
        """
        Active sub-collections for a top-level collection (explore page tags).
        GET .../classes/collections/<slug>/children/
        """
        parent = get_object_or_404(
            ClassCollection.objects.filter(parent__isnull=True, is_active=True),
            slug=parent_slug,
        )
        qs = annotate_collection_active_class_count(
            ClassCollection.objects.filter(parent=parent, is_active=True).order_by(
                "sort_order", "name"
            )
        )
        serializer = PublicCollectionSerializer(
            qs, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    @method_decorator(vary_on_headers("Authorization"))
    def homepage_content(self, request):
        """
        Custom endpoint for homepage data.
        Returns:
        1. Trending classes (Top by relevance)
        2. Date Night classes (Specific collection) - Randomized AND De-duplicated
        3. All Collections/Categories (Metadata pills)
        Cache: mode=collections uses key HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY; invalidated when a collection changes.
        """
        mode = request.query_params.get("mode", "categories")
        if mode == "collections" and not request.user.is_authenticated:
            cached = cache.get(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
            if cached is not None:
                return Response(cached)

        data = {}
        cached_sections = None
        if not request.user.is_authenticated:
            cached_sections = cache.get(HOMEPAGE_SECTIONS_CACHE_KEY)
        if cached_sections is not None:
            data.update(cached_sections)
        else:
            # 1. Base Query with availability check
            base_qs = self.get_queryset()

            # EFFICIENT FILTER: Check for future availability
            future_instances = ScheduleInstance.objects.filter(
                schedule__option__classId=OuterRef("pk"),
                date__gte=timezone.now().date(),
                status="scheduled",
            )
            base_qs = base_qs.filter(Exists(future_instances))

            base_qs = self._calculate_relevance_score(base_qs)

            context = self.get_serializer_context()

            # 2. Trending (Highest Relevance) - Fetch this FIRST
            trending_qs = base_qs.order_by("-relevance_score", "-classId")[:10]
            trending_data = HomepageClassSerializer(
                trending_qs, many=True, context=context
            ).data
            data["trending"] = trending_data

            trending_ids = [item["classId"] for item in trending_data]

            # 3. Date Night Collection
            date_night_slug = "date-night"
            date_night_qs = base_qs.filter(collections__slug=date_night_slug)
            if trending_ids:
                date_night_qs = date_night_qs.exclude(pk__in=trending_ids)
            date_night_qs = date_night_qs.order_by("?")[:10]
            data["date_night"] = HomepageClassSerializer(
                date_night_qs, many=True, context=context
            ).data

            # 3b. Next Week
            today = timezone.now().date()
            days_until_next_monday = (7 - today.weekday()) % 7
            if days_until_next_monday == 0:
                days_until_next_monday = 7
            next_week_start = today + timedelta(days=days_until_next_monday)
            next_week_end = next_week_start + timedelta(days=6)
            next_week_instances = ScheduleInstance.objects.filter(
                schedule__option__classId=OuterRef("pk"),
                date__gte=next_week_start,
                date__lte=next_week_end,
                status="scheduled",
            )
            next_week_qs = base_qs.filter(Exists(next_week_instances)).order_by(
                "-review_count", "-average_rating", "-classId"
            )[:10]
            next_week_class_ids = list(next_week_qs.values_list("pk", flat=True))
            soonest_per_class = {}
            if next_week_class_ids:
                soonest_instances = (
                    ScheduleInstance.objects.filter(
                        schedule__option__classId__in=next_week_class_ids,
                        date__gte=next_week_start,
                        date__lte=next_week_end,
                        status="scheduled",
                    )
                    .order_by("schedule__option__classId", "date", "time")
                    .distinct("schedule__option__classId")
                    .values("schedule__option__classId", "date", "time")
                )
                for row in soonest_instances:
                    soonest_per_class[row["schedule__option__classId"]] = {
                        "date": row["date"],
                        "time": row["time"],
                    }
            next_week_context = {**context, "soonest_per_class": soonest_per_class}
            data["next_week"] = HomepageClassSerializer(
                next_week_qs, many=True, context=next_week_context
            ).data

            if not request.user.is_authenticated:
                _safe_cache_set(
                    HOMEPAGE_SECTIONS_CACHE_KEY,
                    {
                        "trending": data["trending"],
                        "date_night": data["date_night"],
                        "next_week": data["next_week"],
                    },
                    timeout=HOMEPAGE_SECTIONS_CACHE_TIMEOUT,
                )

        context = self.get_serializer_context()

        # 4. Mode Selection: only collections are used (categories removed from platform)
        if mode == "collections":
            child_prefetch = Prefetch(
                "children",
                queryset=ClassCollection.objects.filter(is_active=True).order_by(
                    "sort_order", "name"
                ),
            )
            base_coll = ClassCollection.objects.filter(
                is_active=True, parent__isnull=True
            )
            featured_qs = annotate_collection_active_class_count(
                base_coll.filter(show_in_featured_categories=True).order_by(
                    "sort_order", "name"
                )
            ).prefetch_related(child_prefetch)
            # Do not fall back to "all collections": an empty featured strip is intentional
            # when every toggle is off—otherwise admins think show_in_featured_categories is ignored.
            serialized_collections = PublicCollectionSerializer(
                featured_qs, many=True, context=context
            ).data

            i_want_qs = annotate_collection_active_class_count(
                base_coll.filter(show_in_i_want=True).order_by(
                    "sort_order", "name"
                )
            ).prefetch_related(child_prefetch)

            def _is_duplicate_all_chip(row):
                slug = (row.get("slug") or "").strip().lower()
                name = (row.get("name") or "").strip().lower()
                return slug in ("", "all") or name == "all"

            withdrawn_all_rows = []
            filtered_collections = []
            for row in serialized_collections:
                if _is_duplicate_all_chip(row):
                    withdrawn_all_rows.append(row)
                else:
                    filtered_collections.append(row)

            def _source_for_synthetic_all_chip(rows):
                """Prefer slug=all, then slug blank, then any duplicate row."""
                if not rows:
                    return None
                for prefer in ("all", ""):
                    for row in rows:
                        if (row.get("slug") or "").strip().lower() == prefer:
                            return row
                return rows[0]

            enrich = _source_for_synthetic_all_chip(withdrawn_all_rows)

            # Synthetic routing chip must keep slug/key empty (& collection= aligns with unset filter).
            # If an admin configures a featured row named/slugged "All", merge its visuals into this chip.
            display_name = str((enrich.get("name") if enrich else "") or "").strip() or "All"
            merged_description = str((enrich.get("description") if enrich else "") or "").strip()

            # Synthetic "All" chip — slug/key forced empty; visuals from DB-backed "All" when present.
            all_chip = {
                "id": None,
                "name": display_name,
                "slug": "",
                "key": "",
                "parent_id": None,
                "has_children": False,
                "children": [],
                "description": merged_description,
                "image_medium_url": enrich.get("image_medium_url") if enrich else None,
                "sort_order": -1,
                "search_aliases": list((enrich or {}).get("search_aliases") or []),
                "is_searchable": bool((enrich or {}).get("is_searchable", True)),
                "show_in_i_want": False,
                "show_in_featured_categories": True,
                "show_on_homepage_rows": True,
                "icon_name": str((enrich or {}).get("icon_name") or "").strip(),
                "color": str((enrich or {}).get("color") or "").strip(),
                "is_all": True,
            }
            data["collections"] = [all_chip] + filtered_collections
            data["collections_i_want"] = PublicCollectionSerializer(
                i_want_qs,
                many=True,
                context=context,
            ).data
            _safe_cache_set(
                HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY,
                data,
                timeout=HOMEPAGE_CONTENT_COLLECTIONS_CACHE_TIMEOUT,
            )
        else:
            # Legacy mode=categories: return empty list so clients don't break
            data["categories"] = []

        return Response(data)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        queryset = queryset.order_by("-search_relevance_score", "-createdAt", "-classId")
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, many=True, context=self.get_serializer_context()
            )
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[IsAuthenticated],
        url_path="toggle-favorite",
        url_name="toggle-favorite",
    )
    def toggle_favorite(self, request, pk=None):
        klass = self.get_object()
        user = request.user
        try:
            if user.favorited.filter(pk=klass.pk).exists():
                user.favorited.remove(klass)
                is_favorited = False
            else:
                user.favorited.add(klass)
                is_favorited = True
            return Response(
                {"status": "success", "is_favorited": is_favorited},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.error(
                f"Error toggling favorite for user {user.email}, class {klass.pk}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An internal error occurred."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _finalize_db_search_response(self, request, response):
        """Sampled Typesense vs Postgres logging when DB is the primary search path."""
        self._maybe_shadow_compare_db_primary(request, response)
        return response

    def _maybe_shadow_compare_db_primary(self, request, response):
        """DB search is only used with force_db_search; Typesense is primary, no TS-vs-DB shadow here."""
        return

    def _schedule_search_shadow_compare(self, request, favorited_ids, ts_payload):
        """Sampled Postgres comparison when Typesense is primary (background)."""
        import random

        rate = getattr(settings, "SEARCH_SHADOW_SAMPLE_RATE", 0.05) or 0.0
        if rate <= 0 or random.random() > rate:
            return
        view = self

        def _worker():
            try:
                from quickstart.services.search_engine_service import (
                    log_search_shadow_diff,
                    normalize_db_search_payload,
                )

                db_resp = view._public_class_search_database(request)
                log_search_shadow_diff(
                    ts_payload,
                    normalize_db_search_payload(db_resp),
                    dict(request.query_params),
                )
            except Exception as e:
                logger.debug("search_shadow ts_primary worker skipped: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    def _public_class_search_database(self, request):
        # --- 1. Parameter Extraction ---
        req_lat_str = request.query_params.get("lat")
        req_lng_str = request.query_params.get("lng")
        location_param_text = request.query_params.get(
            "location"
        ) or request.query_params.get("location_search", "")
        search_name = location_param_text.split(",")[0].strip()
        _collection_params_raw = request.query_params.getlist("collection")
        _collection_slugs = []
        _seen_coll = set()
        for _s in _collection_params_raw:
            if not _s:
                continue
            _t = str(_s).strip()
            if not _t:
                continue
            _k = _t.lower()
            if _k not in _seen_coll:
                _seen_coll.add(_k)
                _collection_slugs.append(_t)
        logger.info(f"--- PUBLIC CLASS SEARCH INITIATED ---")
        logger.info(f"Params: {request.query_params}")

        self._geo_search_notice_candidate = None

        req_radius_km_str = request.query_params.get("radius")
        keyword_query_text = request.query_params.get("keyword")
        tag_filter = request.query_params.get("tag")
        price_min_str = request.query_params.get("price_min")
        price_max_str = request.query_params.get("price_max")

        resolved_collection_meta = None
        effective_collection_slugs = list(_collection_slugs)
        if keyword_query_text and not _collection_slugs:
            _matched_coll = match_collection_by_alias(keyword_query_text.strip())
            if _matched_coll:
                effective_collection_slugs = [_matched_coll.slug]
                resolved_collection_meta = {
                    "slug": _matched_coll.slug,
                    "name": _matched_coll.name,
                }
            
        # --- DATE PARAMETERS ---
        req_date_str = request.query_params.get("date")
        req_start_date_str = request.query_params.get("start_date")
        req_end_date_str = request.query_params.get("end_date")

        req_participants_str = request.query_params.get("participants")
        time_preferences = request.query_params.getlist("time_preference")
        sort_by = request.query_params.get("sort_by", "relevance")

        # Get Base Queryset
        queryset = self.get_queryset()

        # EFFICIENT FILTER: Filter out classes with no upcoming schedules
        # Using Exists avoids duplicating rows and simplifies the SQL plan
        has_future_instances = ScheduleInstance.objects.filter(
            schedule__option__classId=OuterRef('pk'),
            date__gte=timezone.now().date(),
            status='scheduled'
        )
        queryset = queryset.filter(Exists(has_future_instances))

        # --- COLLECTION FILTERING ---
        if effective_collection_slugs:
            if len(effective_collection_slugs) == 1:
                _slug_one = effective_collection_slugs[0]
                logger.info(
                    f"Applying Collection Filter: '{_slug_one}'"
                    + (
                        f" (resolved from keyword)"
                        if resolved_collection_meta
                        else ""
                    )
                )
                queryset = queryset.filter(collections__slug=_slug_one)
            else:
                logger.info(
                    "Applying Collection Filter (OR): %s",
                    effective_collection_slugs,
                )
                queryset = queryset.filter(
                    collections__slug__in=effective_collection_slugs
                )

            queryset = queryset.distinct()

        sub_slugs = [
            s.strip()
            for s in request.query_params.getlist("sub")
            if s and str(s).strip()
        ]
        if sub_slugs:
            logger.info("Applying sub-collection filter: %s", sub_slugs)
            queryset = queryset.filter(collections__slug__in=sub_slugs).distinct()

        booking_norm = normalize_booking_type_query(request.query_params.get("class_type"))
        if booking_norm:
            logger.info("Applying booking_type filter: %s", booking_norm)
            queryset = queryset.filter(options__booking_type=booking_norm).distinct()

        queryset = queryset.annotate(
            effective_listing_floor=Case(
                When(
                    Q(min_session_price__isnull=False)
                    & Q(min_course_price__isnull=False),
                    then=Least(F("min_session_price"), F("min_course_price")),
                ),
                When(min_session_price__isnull=False, then=F("min_session_price")),
                When(min_course_price__isnull=False, then=F("min_course_price")),
                default=None,
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
        )

        user_location_point = None
        is_province_search = False
        metro_area_handled = False

        # --- 2. Geographic Search Logic ---
        boundary = None
        normalized_province = normalize_province_name(location_param_text)

        if normalized_province:
            is_province_search = True
            logger.info(
                f"Performing province-wide search for: '{normalized_province.title()}'"
            )
            province_abbr = CANADIAN_PROVINCES.get(normalized_province, "").upper()
            province_full_title = normalized_province.title()
            province_q = Q(businessId__businessState__iexact=province_full_title)
            if province_abbr:
                province_q |= Q(businessId__businessState__iexact=province_abbr)
            queryset = queryset.filter(province_q)
            if req_lat_str and req_lng_str:
                try:
                    user_location_point = Point(
                        float(req_lng_str), float(req_lat_str), srid=4326
                    )
                except (ValueError, TypeError):
                    user_location_point = None

        elif search_name and _is_toronto_gta_search_name(search_name):
            # No single GTA geometry in DB: radius from downtown Toronto (matches PRESET_LOCATIONS).
            metro_area_handled = True
            metro_point = _toronto_gta_center_point()
            metro_radius_km = _toronto_gta_radius_km(req_radius_km_str)
            queryset = queryset.filter(
                point__distance_lte=(metro_point, D(km=metro_radius_km))
            )
            logger.info(
                "GTA metro radius search for %r: %skm from Toronto center",
                search_name,
                metro_radius_km,
            )
            if req_lat_str and req_lng_str:
                try:
                    user_location_point = Point(
                        float(req_lng_str), float(req_lat_str), srid=4326
                    )
                except (ValueError, TypeError):
                    user_location_point = metro_point
            else:
                user_location_point = metro_point

        elif search_name:
            boundary = GeographicBoundary.objects.filter(
                Q(name__iexact=search_name)
                | Q(name__istartswith=f"{search_name} (")
            ).first()

        if not metro_area_handled and boundary:
            within_q = queryset.filter(point__within=boundary.geom)
            if within_q.exists():
                queryset = within_q
                logger.info(
                    "Performing boundary polygon search for %r.",
                    boundary.name,
                )
                if req_lat_str and req_lng_str:
                    try:
                        user_location_point = Point(
                            float(req_lng_str), float(req_lat_str), srid=4326
                        )
                    except (ValueError, TypeError):
                        user_location_point = None
                if user_location_point is None:
                    try:
                        c = boundary.geom.centroid
                        if c:
                            user_location_point = Point(float(c.x), float(c.y), srid=4326)
                    except Exception:
                        user_location_point = None
            else:
                states = business_states_for_province_field(boundary.province)
                if not states:
                    queryset = queryset.none()
                    logger.warning(
                        "Boundary %r has empty province; cannot run nearest-in-province fallback.",
                        boundary.name,
                    )
                else:
                    prov_q = Q()
                    for st in states:
                        prov_q |= Q(businessId__businessState__iexact=st)
                    queryset = queryset.filter(prov_q)
                    sort_by = "distance"
                    try:
                        cen = boundary.geom.centroid
                        if cen:
                            user_location_point = Point(
                                float(cen.x), float(cen.y), srid=4326
                            )
                    except Exception:
                        user_location_point = None
                    self._geo_search_notice_candidate = {
                        "kind": "boundary_empty_nearest_in_province",
                        "area_label": boundary.name,
                    }
                    logger.info(
                        "No classes in boundary %r; nearest matches in province %s",
                        boundary.name,
                        states,
                    )

        elif not metro_area_handled and req_lat_str and req_lng_str and not is_province_search:
            try:
                user_location_point = Point(
                    float(req_lng_str), float(req_lat_str), srid=4326
                )
                search_radius_km = (
                    float(req_radius_km_str)
                    if req_radius_km_str
                    and req_radius_km_str.replace(".", "", 1).isdigit()
                    else float(DEFAULT_SEARCH_RADIUS_KM)
                )
                logger.info(
                    f"Performing radius search: {search_radius_km}km around {req_lat_str}, {req_lng_str}"
                )
                queryset = queryset.filter(
                    point__distance_lte=(
                        user_location_point,
                        D(km=search_radius_km),
                    )
                )
            except (ValueError, TypeError):
                logger.warning(
                    f"Invalid geo params for radius search: lat='{req_lat_str}', lng='{req_lng_str}'"
                )
                user_location_point = None

        # --- 3. Standard Field Filtering ---
        if keyword_query_text:
            logger.info(f"Applying Keyword Search: '{keyword_query_text}'")
            search_query = SearchQuery(
                keyword_query_text, search_type="websearch", config="english"
            )
            queryset = queryset.annotate(
                rank=SearchRank(F("search_vector"), search_query)
            ).filter(search_vector=search_query)
        else:
            queryset = queryset.annotate(rank=Value(0.0, output_field=FloatField()))

        if tag_filter:
            queryset = queryset.filter(options__tags__contains=tag_filter.lower())

        if price_max_str:
            try:
                price_max_decimal = Decimal(price_max_str)
                queryset = queryset.filter(
                    Q(effective_listing_floor__lte=price_max_decimal)
                    | Q(effective_listing_floor__isnull=True)
                )
            except InvalidOperation:
                logger.warning(f"Invalid price_max value: {price_max_str}")

        if price_min_str:
            try:
                price_min_decimal = Decimal(price_min_str)
                if price_min_decimal > 0:
                    queryset = queryset.filter(
                        effective_listing_floor__gte=price_min_decimal
                    )
            except InvalidOperation:
                logger.warning(f"Invalid price_min value: {price_min_str}")

        # --- 4. Availability Filtering ---
        # Updated to check for start/end date params
        apply_availability_filters = bool(req_date_str or req_start_date_str or req_end_date_str or time_preferences)

        if apply_availability_filters:
            logger.info("Applying Availability Filters")
            instance_filters = Q(options__schedules__instances__status="scheduled")

            # Date Range Logic
            if req_start_date_str and req_end_date_str:
                try:
                    start_date = datetime.strptime(req_start_date_str, "%Y-%m-%d").date()
                    end_date = datetime.strptime(req_end_date_str, "%Y-%m-%d").date()
                        
                    # Use range filter on the instance date
                    instance_filters &= Q(
                        options__schedules__instances__date__range=(start_date, end_date)
                    )
                    logger.info(f"Filtering by date range: {start_date} to {end_date}")
                except ValueError:
                    logger.warning(f"Invalid date range: {req_start_date_str} - {req_end_date_str}")
                    instance_filters &= Q(
                        options__schedules__instances__date__gte=timezone.now().date()
                    )
            elif req_date_str:
                try:
                    target_date = datetime.strptime(req_date_str, "%Y-%m-%d").date()
                    instance_filters &= Q(
                        options__schedules__instances__date=target_date
                    )
                except ValueError:
                    instance_filters &= Q(
                        options__schedules__instances__date__gte=timezone.now().date()
                    )
            else:
                # If time prefs exist but no date, default to today onwards
                instance_filters &= Q(
                    options__schedules__instances__date__gte=timezone.now().date()
                )

            if time_preferences:
                time_ranges = {
                    "Morning (6am-12pm)": (time(6, 0), time(11, 59, 59)),
                    "Afternoon (12pm-5pm)": (time(12, 0), time(16, 59, 59)),
                    "Evening (5pm-10pm)": (time(17, 0), time(21, 59, 59)),
                }
                time_range_filters = Q()
                for pref in time_preferences:
                    if pref in time_ranges:
                        start_time, end_time = time_ranges[pref]
                        time_range_filters |= Q(
                            options__schedules__instances__time__range=(
                                start_time,
                                end_time,
                            )
                        )
                if time_range_filters:
                    instance_filters &= time_range_filters

            if (
                req_participants_str
                and req_participants_str.isdigit()
                and int(req_participants_str) > 0
            ):
                instance_filters &= Q(
                    options__schedules__instances__max_participants__gte=int(
                        req_participants_str
                    )
                )

            queryset = queryset.filter(instance_filters).distinct()

        # --- 5. Annotation and Sorting ---
        queryset = queryset.annotate(relevance_score=F("search_relevance_score"))

        if user_location_point:
            queryset = queryset.annotate(
                distance=Distance("point", user_location_point)
            )

        # NOTE: every branch below appends "-classId" as a deterministic
        # tiebreaker. Without it, ties in non-unique fields (relevance_score,
        # rating, price, distance, etc.) combined with LIMIT/OFFSET +
        # DISTINCT cause classes to be skipped or duplicated between pages,
        # which made infinite scroll silently drop results.
        if sort_by == "distance" and user_location_point:
            queryset = queryset.order_by("distance", "-classId")
        elif sort_by == "price_asc":
            queryset = queryset.order_by(
                F("min_session_price").asc(nulls_last=True),
                F("min_course_price").asc(nulls_last=True),
                "-classId",
            )
        elif sort_by == "price_desc":
            queryset = queryset.order_by(
                F("min_session_price").desc(nulls_first=True),
                F("min_course_price").desc(nulls_first=True),
                "-classId",
            )
        elif sort_by == "rating":
            queryset = queryset.order_by(
                "-average_rating", "-review_count", "-classId"
            )
        elif sort_by == "reviews":
            queryset = queryset.order_by(
                "-review_count", "-average_rating", "-classId"
            )
        elif sort_by == "newest":
            queryset = queryset.order_by("-createdAt", "-classId")
        else:  # Default sort is 'relevance'
            order_fields = ["-relevance_score", "-createdAt", "-classId"]
            if keyword_query_text:
                order_fields.insert(0, "-rank")
            queryset = queryset.order_by(*order_fields)

        # --- 6. Pagination and Response ---
        # count_only: one COUNT query only. Otherwise Paginator.count would duplicate
        # the same expensive COUNT this queryset already pays for in paginate_queryset.
        if _is_count_only_request(request):
            final_count = queryset.count()
            logger.info("Final queryset count (count_only): %s", final_count)
            if final_count == 0 and effective_collection_slugs:
                logger.warning(
                    "Collection filter %s returned 0 results. Check if slugs exist in DB.",
                    effective_collection_slugs,
                )
            cand = getattr(self, "_geo_search_notice_candidate", None)
            payload_co = {"count": final_count}
            if cand and final_count > 0:
                payload_co["geo_search_notice"] = cand
            self._geo_search_notice_candidate = None
            return self._finalize_db_search_response(
                request, Response(payload_co)
            )

        page = self.paginate_queryset(queryset)
        if page is not None:
            total = self.paginator.page.paginator.count
            logger.info("Final queryset count (paginated): %s", total)
            if total == 0 and effective_collection_slugs:
                logger.warning(
                    "Collection filter %s returned 0 results. Check if slugs exist in DB.",
                    effective_collection_slugs,
                )
            serializer = self.get_serializer(
                page, many=True, context=self.get_serializer_context()
            )
            response = self.get_paginated_response(serializer.data)
            if resolved_collection_meta and isinstance(response.data, dict):
                response.data = _attach_resolved_collection_to_search_payload(
                    response.data, resolved_collection_meta
                )
            cand = getattr(self, "_geo_search_notice_candidate", None)
            if cand and total > 0 and isinstance(response.data, dict):
                response.data["geo_search_notice"] = cand
            self._geo_search_notice_candidate = None
            return self._finalize_db_search_response(request, response)

        serializer = self.get_serializer(
            queryset, many=True, context=self.get_serializer_context()
        )
        response = Response(serializer.data)
        if resolved_collection_meta and isinstance(response.data, dict):
            response.data = _attach_resolved_collection_to_search_payload(
                response.data, resolved_collection_meta
            )
        self._geo_search_notice_candidate = None
        return self._finalize_db_search_response(request, response)

    @action(detail=False, methods=["get"], url_path="search")
    def search(self, request):
        """
        Handles class searches with a hybrid approach:
        - Uses precise polygon boundaries for known city/area searches.
        - "Toronto" / "Toronto (...)" uses a fixed GTA-wide radius from downtown (no merged polygon).
        - Handles province-wide searches.
        - Falls back to a radius search for specific addresses or landmarks.
        - Unless force_db_search is set, Typesense is the only path: no silent Postgres fallback
          on failure or misconfiguration.
        - Caches results for preset (banner) locations; cache invalidates when classes/schedules change.
        """
        try:
            force_db = str(
                request.query_params.get("force_db_search", "")
            ).lower() in ("1", "true", "yes")
            if not force_db:
                from quickstart.services.typesense_client import typesense_available
                from quickstart.services.search_engine_service import (
                    run_public_class_search,
                )

                if not typesense_available():
                    if getattr(settings, "SEARCH_LOCAL_DB_FALLBACK", False):
                        logger.warning(
                            "Typesense unavailable; using Postgres search (local fallback)."
                        )
                        return self._public_class_search_database(request)
                    return Response(
                        {
                            "error": "Search service is temporarily unavailable.",
                        },
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

                ctx = self.get_serializer_context()
                fav = ctx.get("favorited_ids") or set()
                from quickstart.services.typesense_client import ServiceUnavailable

                try:
                    payload = run_public_class_search(request, favorited_ids=fav)
                    if not _is_count_only_request(request):
                        self._schedule_search_shadow_compare(request, fav, payload)
                    if _is_count_only_request(request):
                        co = Response({"count": payload.get("count", 0)})
                        cc = getattr(settings, "SEARCH_EDGE_CACHE_CONTROL", "")
                        if cc:
                            co["Cache-Control"] = cc
                        co["Cache-Tag"] = "classes-search"
                        return co
                    resp = Response(payload)
                    cc = getattr(settings, "SEARCH_EDGE_CACHE_CONTROL", "")
                    if cc:
                        resp["Cache-Control"] = cc
                    resp["Cache-Tag"] = "classes-search"
                    return resp
                except ServiceUnavailable:
                    logger.warning(
                        "Typesense returned 503 (Not Ready or Lagging); "
                        "returning service unavailable response."
                    )
                    if getattr(settings, "SEARCH_LOCAL_DB_FALLBACK", False):
                        logger.warning(
                            "Typesense 503; using Postgres search (local fallback)."
                        )
                        return self._public_class_search_database(request)
                    return Response(
                        {"error": "Search service is temporarily unavailable."},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                except Exception:
                    logger.exception("Typesense search failed")
                    if getattr(settings, "SEARCH_LOCAL_DB_FALLBACK", False):
                        logger.warning(
                            "Typesense search error; using Postgres search (local fallback)."
                        )
                        return self._public_class_search_database(request)
                    return Response(
                        {"error": "Search service is temporarily unavailable."},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

            return self._public_class_search_database(request)

        except Exception as e:
            logger.error(f"Public class search error: {str(e)}", exc_info=True)
            return Response(
                {"error": "An error occurred during search."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

class PublicScheduleViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = ScheduleSerializer
    queryset = Schedule.objects.filter(
        option__classId__status="active",
        option__classId__businessId__isActive=True,
        option__classId__businessId__verificationStatus="verified",
    ).select_related("option", "option__classId", "option__classId__businessId")

    @action(detail=False, methods=["get"], url_path="availability")
    def availability(self, request, *args, **kwargs):
        option_id_str = request.query_params.get("option_id")
        start_date_str = request.query_params.get("start_date")
        end_date_str = request.query_params.get("end_date")
        if not all([option_id_str, start_date_str, end_date_str]):
            return Response(
                {"error": "'option_id', 'start_date', and 'end_date' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            option_id = int(option_id_str)
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid option_id or date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            option = get_object_or_404(
                ClassOption.objects.select_related("classId__businessId"),
                pk=option_id,
                classId__status="active",
                classId__businessId__isActive=True,
                classId__businessId__verificationStatus="verified",
            )
        except Http404:
            return Response(
                {"error": "Class option not found or is not available."},
                status=status.HTTP_404_NOT_FOUND,
            )
        instances_in_range = (
            ScheduleInstance.objects.filter(
                schedule__option_id=option_id,
                date__range=[start_date, end_date],
                status="scheduled",
            )
            .annotate(
                confirmed_participants=Coalesce(
                    Subquery(
                        Booking.objects.filter(
                            schedule_instance=OuterRef("pk"), status="confirmed"
                        )
                        .values("schedule_instance")
                        .annotate(total_pax=Sum("participants"))
                        .values("total_pax")
                    ),
                    0,
                    output_field=IntegerField(),
                )
            )
            .order_by("date", "time")
        )
        availability_by_date = {}
        for instance in instances_in_range:
            available_spots = (
                instance.max_participants - instance.confirmed_participants
            )
            if available_spots > 0:
                date_str = instance.date.isoformat()
                if date_str not in availability_by_date:
                    availability_by_date[date_str] = []
                availability_by_date[date_str].append(
                    {
                        "time": instance.time.strftime("%H:%M:%S"),
                        "available_spots": available_spots,
                        "instance_id": instance.id,
                        "price": str(instance.price),
                        "duration": instance.duration,
                        "min_participants": instance.min_participants,
                    }
                )
        return Response(availability_by_date)


@api_view(["GET"])
@permission_classes([AllowAny])
def paginated_class_reviews(request, identifier):
    """
    Endpoint for paginated reviews (both platform and Google reviews).
    Accepts either class ID (numeric) or slug.

    Query params:
    - page: Page number (default: 1)
    - page_size: Number of reviews per page (default: 10, max: 50)
    """
    try:
        # Get and validate pagination parameters
        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 10)), 50)

        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 10

        # Get the class - handle both ID and slug
        queryset = ClassesMain.objects.select_related("businessId").filter(
            status="active",
            businessId__isActive=True,
            businessId__verificationStatus="verified",
        )

        if identifier.isdigit():
            class_obj = get_object_or_404(queryset, pk=identifier)
        else:
            from quickstart.utils.class_slug_utils import resolve_class_by_slug

            try:
                class_obj = resolve_class_by_slug(queryset, identifier)
            except ClassesMain.DoesNotExist:
                raise Http404 from None

        business_name = (
            class_obj.businessId.businessName if class_obj.businessId_id else None
        )
        google_review_context = {"business_name": business_name}

        # Cheap totals — do not load all rows into memory (fixes timeouts on large imports).
        platform_base = Reviews.objects.filter(classId=class_obj, status="approved")
        google_base = ImportedGoogleReview.objects.filter(
            business=class_obj.businessId
        )
        platform_count = platform_base.count()
        google_count = google_base.count()
        total_count = platform_count + google_count

        if total_count == 0:
            return Response(
                {
                    "reviews": [],
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "total_count": 0,
                        "has_more": False,
                        "total_pages": 0,
                    },
                    "counts": {
                        "platform_reviews": 0,
                        "google_reviews": 0,
                        "total_reviews": 0,
                    },
                }
            )

        # Merge-sort keys in the database: UNION ALL + ORDER BY + LIMIT/OFFSET for this page only.
        platform_keys = platform_base.annotate(
            sort_date=F("createdAt"),
            kind=Value("p", output_field=CharField(max_length=1)),
            rid=Cast(F("reviewId"), CharField(max_length=36)),
        ).values("sort_date", "kind", "rid")

        google_keys = google_base.annotate(
            sort_date=Coalesce(F("review_date"), F("created_at")),
            kind=Value("g", output_field=CharField(max_length=1)),
            rid=Cast(F("id"), CharField(max_length=36)),
        ).values("sort_date", "kind", "rid")

        combined = platform_keys.union(google_keys, all=True).order_by("-sort_date")

        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        page_rows = list(combined[start_index:end_index])

        platform_ids = []
        google_ids = []
        for row in page_rows:
            if row["kind"] == "p":
                platform_ids.append(int(row["rid"]))
            else:
                google_ids.append(UUID(row["rid"]))

        platform_by_pk = {}
        if platform_ids:
            for rev in Reviews.objects.filter(reviewId__in=platform_ids).select_related(
                "userId"
            ):
                platform_by_pk[rev.reviewId] = rev

        google_by_pk = {}
        if google_ids:
            for rev in ImportedGoogleReview.objects.filter(
                id__in=google_ids
            ).select_related("business"):
                google_by_pk[rev.id] = rev

        paginated_reviews = []
        for row in page_rows:
            if row["kind"] == "p":
                review = platform_by_pk[int(row["rid"])]
                paginated_reviews.append(
                    {
                        **PublicReviewSerializer(review).data,
                        "source": "classeasily",
                        "id": f"p-{review.reviewId}",
                        "date": review.createdAt.isoformat(),
                    }
                )
            else:
                review = google_by_pk[UUID(row["rid"])]
                date_val = review.review_date or review.created_at
                paginated_reviews.append(
                    {
                        **ImportedGoogleReviewSerializer(
                            review, context=google_review_context
                        ).data,
                        "source": "google",
                        "id": f"g-{review.google_review_id}",
                        "date": date_val.isoformat() if date_val else "",
                    }
                )

        has_more = end_index < total_count

        return Response(
            {
                "reviews": paginated_reviews,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total_count": total_count,
                    "has_more": has_more,
                    "total_pages": (total_count + page_size - 1) // page_size,
                },
                "counts": {
                    "platform_reviews": platform_count,
                    "google_reviews": google_count,
                    "total_reviews": total_count,
                },
            }
        )

    except ValueError:
        return Response(
            {"error": "Invalid page or page_size parameter"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(f"Error fetching paginated reviews: {str(e)}", exc_info=True)
        return Response(
            {"error": "An error occurred while fetching reviews"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )