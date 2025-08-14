import math
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
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
from decimal import Decimal, InvalidOperation
import logging
from urllib.parse import quote
from datetime import (
    datetime,
    time,
)

from quickstart.models import (
    ClassImage,
    ClassesMain,
    ClassOption,
    GeographicBoundary,
    Reviews,
    Booking,
    Schedule,
    ScheduleInstance,
)
from quickstart.serializers import (
    PublicClassSerializer,
    ScheduleSerializer,
    PublicClassDetailSerializer,
)
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D  # D is for Distance object

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_RADIUS_KM = 50
W_FEATURED = 1.5
W_QUALITY = 1.0
W_RATING = 0.8
W_REVIEW_COUNT = 0.5
W_NEWNESS = 0.7
QUALITY_SCORE_MAX_DESCRIPTION_LEN = 1000
QUALITY_SCORE_BASE_IMAGES = 5
QUALITY_SCORE_IDEAL_IMAGES = 10
RECENCY_HALFLIFE_DAYS = 90
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
    page_size = 12
    page_size_query_param = "page_size"
    max_page_size = 48


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

    @method_decorator(cache_page(60 * 15))
    @method_decorator(vary_on_headers("Authorization"))
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

    def get_queryset(self):
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
            .annotate(
                average_rating=Coalesce(
                    self.AVERAGE_RATING_SUBQUERY, Value(Decimal("0.0"))
                ),
                review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
                min_session_price=Coalesce(self.MIN_SESSION_PRICE_SUBQUERY, None),
                min_course_price=Coalesce(self.MIN_COURSE_PRICE_SUBQUERY, None),
                image_count=Count("images", distinct=True),
            )
        )

        if self.action == "retrieve":
            logger.debug(
                f"Action is 'retrieve', prefetching schedules for class detail."
            )
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

    def search(self, request):
        """
        Handles class searches with a hybrid approach:
        - Uses precise polygon boundaries for known city/area searches.
        - Handles province-wide searches.
        - Falls back to a radius search for specific addresses or landmarks.
        """
        try:
            logger.debug(
                f"Public class search initiated with params: {request.query_params}"
            )

            # --- 1. Parameter Extraction ---
            req_lat_str = request.query_params.get("lat")
            req_lng_str = request.query_params.get("lng")
            location_param_text = request.query_params.get(
                "location"
            ) or request.query_params.get("location_search", "")
            search_name = location_param_text.split(",")[0].strip()
            search_name_lower = search_name.lower()
            req_radius_km_str = request.query_params.get("radius")
            keyword_query_text = request.query_params.get("keyword")
            tag_filter = request.query_params.get("tag")
            category_key = request.query_params.get("category_key")
            subcategory_key = request.query_params.get("subcategory_key")
            price_max_str = request.query_params.get("price_max")
            req_date_str = request.query_params.get("date")
            req_participants_str = request.query_params.get("participants")
            time_preferences = request.query_params.getlist("time_preference")
            sort_by = request.query_params.get("sort_by", "relevance")

            queryset = self.get_queryset()
            user_location_point = None
            is_province_search = False

            # --- 2. Geographic Search Logic ---
            boundary = None

            # --- MODIFICATION: Enhanced province detection ---
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

                # Set user location point if coordinates are provided (for sorting/distance)
                if req_lat_str and req_lng_str:
                    try:
                        user_location_point = Point(
                            float(req_lng_str), float(req_lat_str), srid=4326
                        )
                    except (ValueError, TypeError):
                        user_location_point = None

            elif search_name:
                # PATH A: KNOWN AREA (POLYGON SEARCH) - Only if not a province search
                boundary = GeographicBoundary.objects.filter(
                    Q(name__iexact=search_name)
                    | Q(name__istartswith=f"{search_name} (")
                ).first()

            if boundary:
                logger.info(
                    f"Performing precise boundary search for: '{boundary.name}' using its stored polygon."
                )
                queryset = queryset.filter(point__within=boundary.geom)
                if req_lat_str and req_lng_str:
                    try:
                        user_location_point = Point(
                            float(req_lng_str), float(req_lat_str), srid=4326
                        )
                    except (ValueError, TypeError):
                        user_location_point = None

            elif req_lat_str and req_lng_str and not is_province_search:
                # PATH B: SPECIFIC POINT (RADIUS SEARCH) - Only if other methods fail and not province search
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
                        f"Performing radius search: {search_radius_km}km around a specific point."
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
            if (
                req_date_str
                or (req_participants_str and req_participants_str.isdigit())
                or time_preferences
            ):
                instance_filters = Q(options__schedules__instances__status="scheduled")
                if req_date_str:
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

            # --- MODIFICATION: Do not sort by distance if it's a wide province search without a user point ---
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
            logger.debug(f"Final queryset count before pagination: {queryset.count()}")
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
                    }
                )
        return Response(availability_by_date)
