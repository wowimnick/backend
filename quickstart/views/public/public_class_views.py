import math
import random
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
    Avg,
    Count,
    Min,
    Max,
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
    Func,
    Prefetch,
    Exists,
)
from django.db.models.functions import (
    Coalesce,
    Power,
    Log,
    Now,
    Extract,
    Sin,
    Cos,
    Radians,
    Cast,
    StrIndex,
    Substr,
    Length,
    ATan2,
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
    time,
    timedelta,
)

from quickstart.models import (
    ClassCategory,
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
    PublicCategorySerializer,
    HomepageClassSerializer
)
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_RADIUS_KM = 80
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

CANADIAN_PROVINCES = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "nova scotia": "NS",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "saskatchewan": "SK",
    # Territories
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
}

PROVINCE_ABBREVIATIONS = {v: k for k, v in CANADIAN_PROVINCES.items()}

# Create comprehensive set of all province identifiers
ALL_PROVINCE_NAMES = set(CANADIAN_PROVINCES.keys()) | set(PROVINCE_ABBREVIATIONS.keys())

# Preset locations used by the frontend banner search (SearchContext GTA_PRESETS). Used to cache
# public class search results so repeated banner searches are fast; cache is invalidated when
# classes/schedules/instances are added or removed.
PRESET_LOCATIONS = {
    "Toronto": (43.6532, -79.3832),
    "Mississauga": (43.589, -79.6441),
    "Brampton": (43.7315, -79.7624),
    "Vaughan": (43.8367, -79.4982),
    "Markham": (43.8561, -79.337),
    "Richmond Hill": (43.8828, -79.4403),
    "Oakville": (43.4675, -79.6877),
    "Burlington": (43.3255, -79.799),
    "Hamilton": (43.2557, -79.8711),
    "Ottawa": (45.4215, -75.6972),
    "Pickering": (43.8374, -79.0863),
    "Ajax": (43.8501, -79.0329),
    "Whitby": (43.8762, -78.9413),
    "Oshawa": (43.8971, -78.8658),
    "Milton": (43.5183, -79.8774),
    "Newmarket": (44.0553, -79.4593),
    "Aurora": (44.0056, -79.4663),
    "Etobicoke": (43.6532, -79.5672),
    "Scarborough": (43.7731, -79.2574),
    "North York": (43.7615, -79.4111),
}
PRESET_CACHE_PREFIX = "public_class_search_preset"
# Version key must match PUBLIC_CLASS_SEARCH_PRESET_VERSION_KEY in quickstart.signals
PRESET_CACHE_VERSION_KEY = "public_class_search_preset_version"
# TTL so old keys expire and Redis does not grow unbounded (OOM). Version invalidation still avoids stale reads.
PRESET_CACHE_TTL = 24 * 60 * 60  # 24 hours
_prewarm_version_local = threading.local()

# Collection-only search cache (no location/category/keyword); same version invalidation as preset
COLLECTION_CACHE_PREFIX = "public_class_search_collection"
# Preset location + collection (e.g. Toronto + trending); same version invalidation
PRESET_COLLECTION_CACHE_PREFIX = "public_class_search_preset_collection"
# Preset location + category (and optional subcategory); same version invalidation
PRESET_CATEGORY_CACHE_PREFIX = "public_class_search_preset_category"
# Category-only or category+subcategory (no location); same version invalidation for explore page
CATEGORY_ONLY_CACHE_PREFIX = "public_class_search_category_only"
PRESET_PREWARM_PAGE_SIZE = 50
# Collections list (homepage_content mode=collections); invalidate when a collection is created/updated/deleted
HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY = "homepage_content_collections"
HOMEPAGE_CONTENT_COLLECTIONS_CACHE_TIMEOUT = 60 * 60  # 1 hour (only used until next collection change)


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


def _is_preset_location_request(search_name, lat_str, lng_str):
    """Return True if (search_name, lat, lng) match a known preset (banner) location."""
    if not search_name:
        return False
    preset = PRESET_LOCATIONS.get(search_name)
    if not preset:
        return False
    try:
        lat, lng = float(lat_str), float(lng_str)
        # Allow small tolerance for float/rounding (e.g. from geocode)
        return abs(lat - preset[0]) < 0.01 and abs(lng - preset[1]) < 0.01
    except (TypeError, ValueError):
        return False


def _build_preset_search_cache_key(request, search_name):
    """Build cache key for a preset location search, or None if not cacheable."""
    # Only cache when there are no extra filters (keyword, tag, category, etc.) so banner-style
    # searches hit the same key and we don't explode key space.
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return None
    if request.query_params.get("category_key") or request.query_params.get("subcategory_key"):
        return None
    if request.query_params.get("price_max") or request.query_params.get("collection"):
        return None
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("page_size", "24")
    participants = request.query_params.get("participants", "1")
    sort_by = request.query_params.get("sort_by", "relevance")
    start_date = request.query_params.get("start_date") or ""
    end_date = request.query_params.get("end_date") or ""
    version = _get_preset_search_cache_version()
    # Normalize location for key (e.g. "North York" -> "north_york")
    location_slug = search_name.lower().replace(" ", "_")
    return f"{PRESET_CACHE_PREFIX}:v{version}:{location_slug}:p{page}:ps{page_size}:n{participants}:s{sort_by}:{start_date}:{end_date}"


def invalidate_public_class_search_preset_cache():
    """
    Call when classes/schedules/instances/bookings change. Does NOT bump version immediately:
    enqueues prewarm with new version; task fills cache for new version then bumps live version,
    so users always get cached responses (never a refetch in front of them).
    """
    try:
        # Use cache directly so we get live version, not thread-local
        version = cache.get(PRESET_CACHE_VERSION_KEY, 0) or 0
        new_version = version + 1
        logger.info(
            "Invalidating public class search preset cache (enqueue prewarm for v%s).",
            new_version,
        )
        from django.conf import settings

        if getattr(settings, "IS_DEPLOYED_ENV", False):
            try:
                from quickstart.tasks.cache_tasks import prewarm_class_search_cache_task

                prewarm_class_search_cache_task.delay(
                    locations=True,
                    collections=True,
                    categories=True,
                    version=new_version,
                )
            except Exception as e:
                logger.warning(
                    "Could not enqueue prewarm after cache invalidation: %s", e
                )
        else:
            # Local/dev: bump immediately so at least cache keys change
            _safe_cache_set(PRESET_CACHE_VERSION_KEY, new_version, timeout=None)
    except Exception as e:
        logger.warning("Failed to invalidate preset search cache: %s", e, exc_info=True)


def _is_collection_only_request(request):
    """Return True if request has only collection filter (no location, keyword, category, etc.)."""
    if not request.query_params.get("collection"):
        return False
    if request.query_params.get("location") or request.query_params.get("location_search"):
        return False
    if request.query_params.get("lat") or request.query_params.get("lng"):
        return False
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return False
    if request.query_params.get("category_key") or request.query_params.get("subcategory_key"):
        return False
    if request.query_params.get("price_max"):
        return False
    return True


def _build_collection_search_cache_key(request, collection_slug):
    """Build cache key for a collection-only search, or None if not cacheable."""
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("page_size", "24")
    participants = request.query_params.get("participants", "1")
    sort_by = request.query_params.get("sort_by", "relevance")
    start_date = request.query_params.get("start_date") or ""
    end_date = request.query_params.get("end_date") or ""
    version = _get_preset_search_cache_version()
    slug = (collection_slug or "").lower().replace(" ", "_")
    return f"{COLLECTION_CACHE_PREFIX}:v{version}:{slug}:p{page}:ps{page_size}:n{participants}:s{sort_by}:{start_date}:{end_date}"


def _build_preset_location_collection_cache_key(request, search_name, collection_slug):
    """Build cache key for preset location + collection (e.g. Toronto + trending)."""
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return None
    if request.query_params.get("category_key") or request.query_params.get("subcategory_key"):
        return None
    if request.query_params.get("price_max"):
        return None
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("page_size", "24")
    participants = request.query_params.get("participants", "1")
    sort_by = request.query_params.get("sort_by", "relevance")
    start_date = request.query_params.get("start_date") or ""
    end_date = request.query_params.get("end_date") or ""
    version = _get_preset_search_cache_version()
    location_slug = (search_name or "").lower().replace(" ", "_")
    coll_slug = (collection_slug or "").lower().replace(" ", "_")
    return f"{PRESET_COLLECTION_CACHE_PREFIX}:v{version}:{location_slug}:{coll_slug}:p{page}:ps{page_size}:n{participants}:s{sort_by}:{start_date}:{end_date}"


def _build_preset_location_category_cache_key(
    request, search_name, category_key, subcategory_key=None
):
    """Build cache key for preset location + category (and optional subcategory)."""
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return None
    if request.query_params.get("collection") or request.query_params.get("price_max"):
        return None
    if not category_key or (category_key or "").lower() == "all":
        return None
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("page_size", "24")
    participants = request.query_params.get("participants", "1")
    sort_by = request.query_params.get("sort_by", "relevance")
    start_date = request.query_params.get("start_date") or ""
    end_date = request.query_params.get("end_date") or ""
    version = _get_preset_search_cache_version()
    location_slug = (search_name or "").lower().replace(" ", "_")
    cat_slug = (category_key or "").lower().replace(" ", "_")
    sub_slug = (subcategory_key or "").lower().replace(" ", "_") if subcategory_key else ""
    return f"{PRESET_CATEGORY_CACHE_PREFIX}:v{version}:{location_slug}:{cat_slug}:{sub_slug}:p{page}:ps{page_size}:n{participants}:s{sort_by}:{start_date}:{end_date}"


def _is_category_only_request(request):
    """Return True if request has only category (and optional subcategory), no location/collection."""
    if request.query_params.get("location") or request.query_params.get("location_search"):
        return False
    if request.query_params.get("lat") or request.query_params.get("lng"):
        return False
    if request.query_params.get("collection"):
        return False
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return False
    if request.query_params.get("price_max"):
        return False
    category_key = request.query_params.get("category_key")
    if not category_key or (category_key or "").lower() == "all":
        return False
    return True


def _build_category_only_cache_key(request, category_key, subcategory_key=None):
    """Build cache key for category-only or category+subcategory (no location). Explore page instant load."""
    if request.query_params.get("keyword") or request.query_params.get("tag"):
        return None
    if request.query_params.get("collection") or request.query_params.get("price_max"):
        return None
    if not category_key or (category_key or "").lower() == "all":
        return None
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("page_size", "24")
    participants = request.query_params.get("participants", "1")
    sort_by = request.query_params.get("sort_by", "relevance")
    start_date = request.query_params.get("start_date") or ""
    end_date = request.query_params.get("end_date") or ""
    version = _get_preset_search_cache_version()
    cat_slug = (category_key or "").lower().replace(" ", "_")
    sub_slug = (subcategory_key or "").lower().replace(" ", "_") if subcategory_key else ""
    return f"{CATEGORY_ONLY_CACHE_PREFIX}:v{version}:{cat_slug}:{sub_slug}:p{page}:ps{page_size}:n{participants}:s{sort_by}:{start_date}:{end_date}"


def normalize_province_name(location_text):
    """
    Normalize province names/abbreviations to full province names for consistent searching.
    Handles cases like "ON, Canada" -> "ontario"
    """
    if not location_text:
        return None

    # Clean the input - remove "Canada" and extra whitespace
    cleaned = location_text.replace(", Canada", "").replace(",Canada", "").strip()
    cleaned_lower = cleaned.lower()

    # Check if it's a province abbreviation
    if cleaned.upper() in PROVINCE_ABBREVIATIONS:
        return PROVINCE_ABBREVIATIONS[cleaned.upper()]

    # Check if it's already a full province name
    if cleaned_lower in CANADIAN_PROVINCES:
        return cleaned_lower

    return None


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
        "category__name",
        "subcategory__name",
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
        else:
            filter_kwargs = {"slug": identifier}
        obj = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
        .values("classId")
        .annotate(avg_rating=Avg("rating"))
        .values("avg_rating")[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1),
    )
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
        .values("classId")
        .annotate(count=Count("reviewId"))
        .values("count")[:1],
        output_field=IntegerField(),
    )
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

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)

        # Log response size
        import json

        response_size = len(json.dumps(response.data))
        logger.info(f"Response size: {response_size / 1024 / 1024:.2f} MB")

        if response_size > 6000000:
            logger.error(f"⚠️ Response exceeds 6MB! Size: {response_size} bytes")

        return response

    def get_queryset(self):
        # 1. Define Subqueries for Platform Data (Existing)
        # We ensure these return Decimal/Integer types to prevent SQL casting errors
        p_avg_subquery = Subquery(
            Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
            .values("classId")
            .annotate(avg_rating=Avg("rating"))
            .values("avg_rating")[:1],
            output_field=DecimalField(max_digits=3, decimal_places=2),
        )
        p_count_subquery = Subquery(
            Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
            .values("classId")
            .annotate(count=Count("reviewId"))
            .values("count")[:1],
            output_field=IntegerField(),
        )

        # 2. Define Subqueries for Google Data (New)
        # Note: We filter by businessId because Google reviews are attached to the Business, not the specific class
        g_avg_subquery = Subquery(
            ImportedGoogleReview.objects.filter(business=OuterRef("businessId"))
            .values("business")
            .annotate(avg=Avg("rating"))
            .values("avg")[:1],
            output_field=DecimalField(max_digits=3, decimal_places=2),
        )
        g_count_subquery = Subquery(
            ImportedGoogleReview.objects.filter(business=OuterRef("businessId"))
            .values("business")
            .annotate(count=Count("id"))
            .values("count")[:1],
            output_field=IntegerField(),
        )

        # 3. Define Price Subqueries (Existing)
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

        queryset = (
            ClassesMain.objects.select_related("businessId", "category", "subcategory")
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=ClassImage.objects.order_by("-isCover", "createdAt"),
                ),
                "options",
            )
            .filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
            )
            # 4. Annotate Raw Counts and Ratings
            .annotate(
                # Coalesce ensures we get 0 instead of NULL if no reviews exist
                p_rating_raw=Coalesce(p_avg_subquery, Value(Decimal("0.00"))),
                p_count_raw=Coalesce(p_count_subquery, Value(0)),
                g_rating_raw=Coalesce(g_avg_subquery, Value(Decimal("0.00"))),
                g_count_raw=Coalesce(g_count_subquery, Value(0)),
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
            logger.debug("Action is 'retrieve', prefetching schedules for class detail.")
            queryset = queryset.prefetch_related(
                Prefetch(
                    "options__schedules",
                    queryset=Schedule.objects.filter(
                        Q(date__gte=timezone.now().date())
                        | Q(end_date__gte=timezone.now().date())
                    ).order_by("date", "time"),
                )
            )
            
        return queryset.distinct()
    
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
        if mode == "collections":
            cached = cache.get(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
            if cached is not None:
                return Response(cached)

        # 1. Base Query with availability check
        base_qs = self.get_queryset()

        # EFFICIENT FILTER: Check for future availability
        future_instances = ScheduleInstance.objects.filter(
            schedule__option__classId=OuterRef('pk'),
            date__gte=timezone.now().date(),
            status='scheduled'
        )
        base_qs = base_qs.filter(Exists(future_instances))
        
        # Calculate scores
        base_qs = self._calculate_relevance_score(base_qs)

        context = {'request': request}
        data = {}

        # 2. Trending (Highest Relevance) - Fetch this FIRST
        trending_qs = base_qs.order_by('-relevance_score')[:10]
        # Serialize immediately to get the IDs
        trending_data = HomepageClassSerializer(trending_qs, many=True, context=context).data
        data["trending"] = trending_data
        
        # Extract IDs to prevent duplicates in the next section
        trending_ids = [item['classId'] for item in trending_data]

        # 3. Date Night Collection
        date_night_slug = "date-night"
        date_night_qs = base_qs.filter(collections__slug=date_night_slug)
        
        # EXCLUDE classes that are already in the Trending list
        if trending_ids:
            date_night_qs = date_night_qs.exclude(pk__in=trending_ids)
            
        # Randomize the remaining results so it's different every time
        date_night_qs = date_night_qs.order_by('?')[:10]
        
        data["date_night"] = HomepageClassSerializer(date_night_qs, many=True, context=context).data

        # 3b. Next Week — classes with at least one schedule in the next calendar week, ordered by most reviews
        today = timezone.now().date()
        # Next Monday (weekday 0); if today is Monday, "next week" starts next Monday
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
            "-review_count", "-average_rating"
        )[:10]
        # Soonest (date, time) per class for "Happening Next Week" cards
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

        # 4. Mode Selection (Categories vs Collections pills)
        if mode == "collections":
            collections_qs = ClassCollection.objects.filter(is_active=True).order_by("sort_order")
            data["collections"] = PublicCollectionSerializer(
                collections_qs, many=True, context=context
            ).data
            _safe_cache_set(
                HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY,
                data,
                timeout=HOMEPAGE_CONTENT_COLLECTIONS_CACHE_TIMEOUT,
            )
        else:
            categories_qs = ClassCategory.objects.all().order_by("sort_order")
            data["categories"] = PublicCategorySerializer(
                categories_qs, many=True, context=context
            ).data

        return Response(data)

    def _calculate_relevance_score(self, queryset):
        # Cast all numeric operations to explicit types for psycopg3 compatibility
        days_old = ExpressionWrapper(
            Extract(Now() - F("createdAt"), "epoch")
            / Cast(Value(86400.0), FloatField()),
            output_field=FloatField(),
        )

        # Fix: Add explicit type casting to ensure consistent numeric types
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
                + (Cast(Value(W_REVIEW_COUNT), FloatField()) * review_count_score)
                + (Cast(Value(W_NEWNESS), FloatField()) * newness_score)
            )
            * featured_multiplier,
            output_field=FloatField(),
        )

        return queryset.annotate(relevance_score=relevance_score)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        queryset = self._calculate_relevance_score(queryset)
        queryset = queryset.order_by("-relevance_score", "-createdAt")
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, many=True, context={"request": request}
            )
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(
            queryset, many=True, context={"request": request}
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

    @action(detail=False, methods=["get"], url_path="search")
    def search(self, request):
        """
        Handles class searches with a hybrid approach:
        - Uses precise polygon boundaries for known city/area searches.
        - Handles province-wide searches.
        - Falls back to a radius search for specific addresses or landmarks.
        - Caches results for preset (banner) locations; cache invalidates when classes/schedules change.
        """
        try:
            # --- 1. Parameter Extraction ---
            req_lat_str = request.query_params.get("lat")
            req_lng_str = request.query_params.get("lng")
            location_param_text = request.query_params.get(
                "location"
            ) or request.query_params.get("location_search", "")
            search_name = location_param_text.split(",")[0].strip()
            collection_slug = request.query_params.get("collection")

            # Preset location + collection cache (e.g. Toronto + trending): return cached if available
            preset_collection_cache_key = None
            preset_category_cache_key = None
            if (
                _is_preset_location_request(search_name, req_lat_str, req_lng_str)
                and collection_slug
                and not request.query_params.get("keyword")
                and not request.query_params.get("tag")
                and not request.query_params.get("category_key")
                and not request.query_params.get("subcategory_key")
                and not request.query_params.get("price_max")
            ):
                preset_collection_cache_key = _build_preset_location_collection_cache_key(
                    request, search_name, collection_slug
                )
                if preset_collection_cache_key:
                    cached = cache.get(preset_collection_cache_key)
                    if cached is not None:
                        logger.info(
                            "Returning cached preset+collection result for %s + %s",
                            search_name,
                            collection_slug,
                        )
                        return Response(cached)

            # Preset (banner) location cache only (no collection): return cached if available
            preset_cache_key = None
            if _is_preset_location_request(search_name, req_lat_str, req_lng_str):
                preset_cache_key = _build_preset_search_cache_key(request, search_name)
                if preset_cache_key:
                    cached = cache.get(preset_cache_key)
                    if cached is not None:
                        logger.info("Returning cached preset search result for %s", search_name)
                        return Response(cached)

            # Preset location + category (and optional subcategory) cache: return cached if available
            _cat_key = request.query_params.get("category_key")
            _sub_key = request.query_params.get("subcategory_key")
            if (
                _is_preset_location_request(search_name, req_lat_str, req_lng_str)
                and (_cat_key or _sub_key)
                and not collection_slug
                and not request.query_params.get("keyword")
                and not request.query_params.get("tag")
                and not request.query_params.get("price_max")
            ):
                preset_category_cache_key = _build_preset_location_category_cache_key(
                    request, search_name, _cat_key or "", _sub_key
                )
                if preset_category_cache_key:
                    cached = cache.get(preset_category_cache_key)
                    if cached is not None:
                        logger.info(
                            "Returning cached preset+category result for %s + %s",
                            search_name,
                            _cat_key or _sub_key,
                        )
                        return Response(cached)

            logger.info(f"--- PUBLIC CLASS SEARCH INITIATED ---")
            logger.info(f"Params: {request.query_params}")
            
            req_radius_km_str = request.query_params.get("radius")
            keyword_query_text = request.query_params.get("keyword")
            tag_filter = request.query_params.get("tag")
            category_key = request.query_params.get("category_key")
            subcategory_key = request.query_params.get("subcategory_key")
            price_max_str = request.query_params.get("price_max")
            
            # --- DATE PARAMETERS ---
            req_date_str = request.query_params.get("date")
            req_start_date_str = request.query_params.get("start_date")
            req_end_date_str = request.query_params.get("end_date")

            req_participants_str = request.query_params.get("participants")
            time_preferences = request.query_params.getlist("time_preference")
            sort_by = request.query_params.get("sort_by", "relevance")

            # Collection-only cache: return cached response if available
            collection_cache_key = None  # set below when request is collection-only
            if _is_collection_only_request(request) and collection_slug:
                collection_cache_key = _build_collection_search_cache_key(request, collection_slug)
                if collection_cache_key:
                    cached = cache.get(collection_cache_key)
                    if cached is not None:
                        logger.info(
                            "Returning cached collection search result for %s",
                            collection_slug,
                        )
                        return Response(cached)

            # Category-only (and category+subcategory) cache: no location, explore page instant load
            category_only_cache_key = None
            _cat_key = request.query_params.get("category_key")
            _sub_key = request.query_params.get("subcategory_key")
            if _is_category_only_request(request) and _cat_key:
                category_only_cache_key = _build_category_only_cache_key(
                    request, _cat_key, _sub_key
                )
                if category_only_cache_key:
                    cached = cache.get(category_only_cache_key)
                    if cached is not None:
                        logger.info(
                            "Returning cached category-only search result for %s%s",
                            _cat_key,
                            f"+{_sub_key}" if _sub_key else "",
                        )
                        return Response(cached)

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
            if collection_slug:
                logger.info(f"Applying Collection Filter: '{collection_slug}'")
                queryset = queryset.filter(collections__slug=collection_slug)
                # Ensure distinctness after M2M filter just in case
                queryset = queryset.distinct()
                
                # Check count after collection filter
                count_after_collection = queryset.count()
                logger.info(f"QuerySet Count after Collection Filter: {count_after_collection}")
                
                if count_after_collection == 0:
                    logger.warning(f"Collection filter '{collection_slug}' returned 0 results. Check if slug exists in DB.")

            user_location_point = None
            is_province_search = False

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

            elif search_name:
                boundary = GeographicBoundary.objects.filter(
                    Q(name__iexact=search_name)
                    | Q(name__istartswith=f"{search_name} (")
                ).first()

            if boundary:
                queryset_in_boundary = queryset.filter(point__within=boundary.geom)
                if queryset_in_boundary.exists():
                    logger.info(
                        f"Performing precise boundary search for: '{boundary.name}' using its stored polygon."
                    )
                    queryset = queryset_in_boundary
                    if req_lat_str and req_lng_str:
                        try:
                            user_location_point = Point(
                                float(req_lng_str), float(req_lat_str), srid=4326
                            )
                        except (ValueError, TypeError):
                            user_location_point = None
                else:
                    # No classes inside boundary (e.g. Newmarket); show classes in nearby areas
                    queryset_before_fallback = queryset
                    try:
                        centroid = boundary.geom.centroid
                        if centroid:
                            nearby_radius_km = (
                                float(req_radius_km_str)
                                if req_radius_km_str
                                and req_radius_km_str.replace(".", "", 1).isdigit()
                                else DEFAULT_SEARCH_RADIUS_KM
                            )
                            queryset = queryset.filter(
                                point__distance_lte=(
                                    centroid,
                                    D(km=nearby_radius_km),
                                )
                            )
                            logger.info(
                                f"No classes in '{boundary.name}'; showing classes within "
                                f"{nearby_radius_km}km of area (nearby neighborhoods)."
                            )
                            # Use user's lat/lng for distance sort when available
                            if req_lat_str and req_lng_str:
                                try:
                                    user_location_point = Point(
                                        float(req_lng_str), float(req_lat_str), srid=4326
                                    )
                                except (ValueError, TypeError):
                                    user_location_point = centroid
                            else:
                                user_location_point = centroid
                    except Exception as e:
                        logger.warning(f"Boundary fallback failed: {e}")
                        queryset = queryset_before_fallback

            elif req_lat_str and req_lng_str and not is_province_search:
                try:
                    user_location_point = Point(
                        float(req_lng_str), float(req_lat_str), srid=4326
                    )
                    search_radius_km = (
                        float(req_radius_km_str)
                        if req_radius_km_str
                        and req_radius_km_str.replace(".", "", 1).isdigit()
                        else 25.0
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

            if category_key and category_key.lower() != "all":
                queryset = queryset.filter(category__key=category_key)
                if subcategory_key:
                    queryset = queryset.filter(subcategory__key=subcategory_key)

            if price_max_str:
                try:
                    price_max_decimal = Decimal(price_max_str)
                    queryset = queryset.filter(
                        Q(min_session_price__lte=price_max_decimal)
                        | Q(min_course_price__lte=price_max_decimal)
                        | (
                            Q(min_session_price__isnull=True)
                            & Q(min_course_price__isnull=True)
                        )
                    )
                except InvalidOperation:
                    logger.warning(f"Invalid price_max value: {price_max_str}")

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
            queryset = self._calculate_relevance_score(queryset)

            if user_location_point:
                queryset = queryset.annotate(
                    distance=Distance("point", user_location_point)
                )

            if sort_by == "distance" and user_location_point:
                queryset = queryset.order_by("distance")
            elif sort_by == "price_asc":
                queryset = queryset.order_by(
                    F("min_session_price").asc(nulls_last=True),
                    F("min_course_price").asc(nulls_last=True),
                )
            elif sort_by == "price_desc":
                queryset = queryset.order_by(
                    F("min_session_price").desc(nulls_first=True),
                    F("min_course_price").desc(nulls_first=True),
                )
            elif sort_by == "rating":
                queryset = queryset.order_by("-average_rating", "-review_count")
            elif sort_by == "reviews":
                queryset = queryset.order_by("-review_count", "-average_rating")
            elif sort_by == "newest":
                queryset = queryset.order_by("-createdAt")
            else:  # Default sort is 'relevance'
                order_fields = ["-relevance_score", "-createdAt"]
                if keyword_query_text:
                    order_fields.insert(0, "-rank")
                queryset = queryset.order_by(*order_fields)

            # --- 6. Pagination and Response ---
            final_count = queryset.count()
            logger.info(f"Final queryset count before pagination: {final_count}")
            
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(
                    page, many=True, context={"request": request}
                )
                response = self.get_paginated_response(serializer.data)
                if preset_collection_cache_key:
                    _safe_cache_set(
                        preset_collection_cache_key,
                        response.data,
                        timeout=PRESET_CACHE_TTL,
                    )
                if preset_cache_key:
                    _safe_cache_set(preset_cache_key, response.data, timeout=PRESET_CACHE_TTL)
                if preset_category_cache_key:
                    _safe_cache_set(
                        preset_category_cache_key,
                        response.data,
                        timeout=PRESET_CACHE_TTL,
                    )
                if collection_cache_key:
                    _safe_cache_set(
                        collection_cache_key, response.data, timeout=PRESET_CACHE_TTL
                    )
                if category_only_cache_key:
                    _safe_cache_set(
                        category_only_cache_key,
                        response.data,
                        timeout=PRESET_CACHE_TTL,
                    )
                return response

            serializer = self.get_serializer(
                queryset, many=True, context={"request": request}
            )
            response = Response(serializer.data)
            if preset_collection_cache_key:
                _safe_cache_set(
                    preset_collection_cache_key,
                    response.data,
                    timeout=PRESET_CACHE_TTL,
                )
            if preset_cache_key:
                _safe_cache_set(preset_cache_key, response.data, timeout=PRESET_CACHE_TTL)
            if preset_category_cache_key:
                _safe_cache_set(
                    preset_category_cache_key,
                    response.data,
                    timeout=PRESET_CACHE_TTL,
                )
            if collection_cache_key:
                _safe_cache_set(
                    collection_cache_key, response.data, timeout=PRESET_CACHE_TTL
                )
            if category_only_cache_key:
                _safe_cache_set(
                    category_only_cache_key, response.data, timeout=PRESET_CACHE_TTL
                )
            return response

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
            class_obj = get_object_or_404(queryset, slug=identifier)

        # Fetch platform reviews
        platform_reviews = (
            Reviews.objects.filter(classId=class_obj, status="approved")
            .select_related("userId")
            .order_by("-createdAt")
        )

        # Fetch Google reviews
        google_reviews = ImportedGoogleReview.objects.filter(
            business=class_obj.businessId
        ).order_by("-review_date")

        # Format reviews with source tags
        formatted_platform_reviews = [
            {
                **PublicReviewSerializer(review).data,
                "source": "classeasily",
                "id": f"p-{review.reviewId}",
                "date": review.createdAt.isoformat(),
            }
            for review in platform_reviews
        ]

        formatted_google_reviews = [
            {
                **ImportedGoogleReviewSerializer(review).data,
                "source": "google",
                "id": f"g-{review.google_review_id}",
                "date": review.review_date.isoformat(),
            }
            for review in google_reviews
        ]

        # Combine and sort all reviews by date
        all_reviews = formatted_platform_reviews + formatted_google_reviews
        all_reviews.sort(key=lambda x: x["date"], reverse=True)

        # Calculate pagination
        total_count = len(all_reviews)
        start_index = (page - 1) * page_size
        end_index = start_index + page_size

        paginated_reviews = all_reviews[start_index:end_index]
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
                    "platform_reviews": len(formatted_platform_reviews),
                    "google_reviews": len(formatted_google_reviews),
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