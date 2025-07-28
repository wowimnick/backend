# quickstart/views/public/public_class_views.py

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

# --- MODIFIED: Added imports for Full-Text Search ---
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank

# --- MODIFIED: Added imports for Caching ---
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers

from django.utils import timezone
from decimal import Decimal, InvalidOperation
import logging
import requests
from urllib.parse import quote
from datetime import (
    datetime,
    time,
    date as datetime_date,
)

from quickstart.models import (
    ClassImage,
    ClassesMain,
    ClassOption,
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
from ..utils import haversine_distance

logger = logging.getLogger(__name__)

# --- REMOVED: Photon API is no longer used in the backend ---
# PHOTON_API_URL = "https://photon.komoot.io/api/"
DEFAULT_SEARCH_RADIUS_KM = 50

# --- REFINED: Relevance Scoring Weights (Tune these values based on business goals) ---
W_FEATURED = 1.5
W_QUALITY = 1.0
W_RATING = 0.8
W_REVIEW_COUNT = 0.5
W_NEWNESS = 0.7

# --- REFINED: Relevance Score Normalization/Tuning Constants ---
QUALITY_SCORE_MAX_DESCRIPTION_LEN = 1000
QUALITY_SCORE_BASE_IMAGES = 5
QUALITY_SCORE_IDEAL_IMAGES = 10
RECENCY_HALFLIFE_DAYS = 90
REVIEW_COUNT_FOR_MAX_SCORE = 50

# --- REMOVED: Backend geocoding function is no longer needed. ---
# This is now handled by the frontend to prevent blocking API calls.


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
        "rank",  # --- ADDED: Allow ordering by search rank ---
    ]
    ordering = ["-createdAt"]

    lookup_field = "pk"

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicClassDetailSerializer
        return super().get_serializer_class()

    def get_object(self):
        """
        Overrides the default `get_object` to allow lookup by either the
        numeric primary key (pk) or the SEO-friendly slug.
        """
        queryset = self.filter_queryset(self.get_queryset())
        identifier = self.kwargs.get(self.lookup_field)
        if identifier.isdigit():
            filter_kwargs = {"pk": identifier}
        else:
            filter_kwargs = {"slug": identifier}
        obj = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, obj)
        return obj

    # --- MODIFIED: Added Caching to the retrieve method ---
    # Caches the class detail page for 15 minutes. Varies by authentication
    # to handle the 'is_favorited' field correctly for different users.
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
                # Prefetch the first 6 approved reviews for the detail view
                Prefetch(
                    "reviews",
                    queryset=Reviews.objects.filter(status="approved").order_by(
                        "-createdAt"
                    )[:6],
                    to_attr="reviews",  # The source for the serializer field
                ),
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
        days_old = Extract(Now() - F("createdAt"), "epoch") / Value(86400.0)

        image_score_numerator = Log(
            10, F("image_count") - Value(QUALITY_SCORE_BASE_IMAGES) + 1
        )
        image_score_denominator = Log(
            10, Value(QUALITY_SCORE_IDEAL_IMAGES - QUALITY_SCORE_BASE_IMAGES) + 1
        )

        image_score = Case(
            When(image_count__gte=QUALITY_SCORE_IDEAL_IMAGES, then=Value(1.0)),
            When(
                image_count__gt=QUALITY_SCORE_BASE_IMAGES,
                then=ExpressionWrapper(
                    image_score_numerator / image_score_denominator,
                    output_field=FloatField(),
                ),
            ),
            default=Value(0.0),
            output_field=FloatField(),
        )

        description_score = Log(10, Length("description") + 1) / Log(
            10, Value(QUALITY_SCORE_MAX_DESCRIPTION_LEN + 1)
        )

        quality_score = ExpressionWrapper(
            (description_score + image_score) / 2.0,
            output_field=FloatField(),
        )

        rating_score = ExpressionWrapper(
            F("average_rating") / Value(5.0), output_field=FloatField()
        )

        review_count_score = ExpressionWrapper(
            Log(10, F("review_count") + 1)
            / Log(10, Value(REVIEW_COUNT_FOR_MAX_SCORE + 1)),
            output_field=FloatField(),
        )

        newness_score = ExpressionWrapper(
            Power(2, -days_old / Value(RECENCY_HALFLIFE_DAYS)),
            output_field=FloatField(),
        )

        featured_multiplier = Case(
            When(businessId__featured=True, then=Value(W_FEATURED)),
            default=Value(1.0),
            output_field=FloatField(),
        )

        relevance_score = ExpressionWrapper(
            (
                (Value(W_QUALITY) * quality_score)
                + (Value(W_RATING) * rating_score)
                + (Value(W_REVIEW_COUNT) * review_count_score)
                + (Value(W_NEWNESS) * newness_score)
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

    @action(detail=False, methods=["get"], permission_classes=[AllowAny])
    def search(self, request):
        try:
            req_lat_str = request.query_params.get("lat")
            req_lng_str = request.query_params.get("lng")
            req_radius_km_str = request.query_params.get("radius")
            # --- REMOVED: `location_search` is no longer used for backend geocoding ---
            # location_search_text = request.query_params.get("location_search")
            keyword_query_text = request.query_params.get("keyword")

            search_lat, search_lng = None, None
            if req_lat_str and req_lng_str:
                try:
                    search_lat, search_lng = float(req_lat_str), float(req_lng_str)
                except (ValueError, TypeError):
                    logger.warning(
                        f"Invalid geo params: lat='{req_lat_str}', lng='{req_lng_str}'"
                    )

            # --- REMOVED: Backend geocoding block ---

            queryset = self.get_queryset()

            # --- MODIFIED: Integrated Full-Text Search ---
            if keyword_query_text:
                # Use 'websearch' for parsing queries like "pottery class" or "art -paint"
                search_query = SearchQuery(
                    keyword_query_text, search_type="websearch", config="english"
                )
                queryset = queryset.annotate(
                    rank=SearchRank(F("search_vector"), search_query)
                ).filter(search_vector=search_query)
            else:
                # Annotate with a null rank if no keyword is provided for consistent ordering
                queryset = queryset.annotate(rank=Value(0.0, output_field=FloatField()))

            category_key = request.query_params.get("category_key")
            subcategory_key = request.query_params.get("subcategory_key")
            if category_key and category_key.lower() != "all":
                queryset = queryset.filter(category__key=category_key)
                if subcategory_key:
                    queryset = queryset.filter(subcategory__key=subcategory_key)

            price_max_str = request.query_params.get("price_max")
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
                    logger.warning(f"Invalid price_max: {price_max_str}")

            req_date_str = request.query_params.get("date")
            req_participants_str = request.query_params.get("participants")
            time_preferences = request.query_params.getlist("time_preference")

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

            if search_lat is not None and search_lng is not None:
                queryset = queryset.exclude(
                    Q(latitude__isnull=True) | Q(longitude__isnull=True)
                )

                db_lat = F("latitude")
                db_lng = F("longitude")

                lat_r = Radians(db_lat)
                lng_r = Radians(db_lng)
                search_lat_r = Radians(Value(search_lat, output_field=FloatField()))
                search_lng_r = Radians(Value(search_lng, output_field=FloatField()))

                d_lng = lng_r - search_lng_r
                d_lat = lat_r - search_lat_r
                a = Power(Sin(d_lat / 2), 2) + Cos(search_lat_r) * Cos(lat_r) * Power(
                    Sin(d_lng / 2), 2
                )
                c = 2 * ATan2(Power(a, 0.5), Power(1 - a, 0.5))
                distance_expr = ExpressionWrapper(6371 * c, output_field=FloatField())

                queryset = queryset.annotate(distance=distance_expr)

                search_radius_km = DEFAULT_SEARCH_RADIUS_KM
                if (
                    req_radius_km_str
                    and req_radius_km_str.replace(".", "", 1).isdigit()
                    and float(req_radius_km_str) > 0
                ):
                    search_radius_km = float(req_radius_km_str)

                queryset = queryset.filter(distance__lte=search_radius_km)

            queryset = self._calculate_relevance_score(queryset)

            # --- MODIFIED: Enhanced sorting logic with Full-Text Search Rank ---
            sort_by = request.query_params.get("sort_by", "relevance")
            if sort_by == "relevance":
                # If a keyword search was performed, prioritize the text match rank.
                # Otherwise, fall back to the general relevance score.
                if keyword_query_text:
                    queryset = queryset.order_by(
                        "-rank", "-relevance_score", "-createdAt"
                    )
                else:
                    queryset = queryset.order_by("-relevance_score", "-createdAt")
            elif sort_by == "distance" and search_lat is not None:
                queryset = queryset.order_by(F("distance").asc(nulls_last=True))
            elif sort_by == "price_asc":
                queryset = queryset.order_by(
                    Coalesce(F("min_session_price"), F("min_course_price")).asc(
                        nulls_last=True
                    )
                )
            elif sort_by == "price_desc":
                queryset = queryset.order_by(
                    Coalesce(F("min_session_price"), F("min_course_price")).desc(
                        nulls_first=True
                    )
                )
            elif sort_by == "rating":
                queryset = queryset.order_by("-average_rating", "-review_count")
            elif sort_by == "reviews":
                queryset = queryset.order_by("-review_count", "-average_rating")
            elif sort_by == "newest":
                queryset = queryset.order_by("-createdAt")

            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(
                    page, many=True, context={"request": request}
                )
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(
                queryset, many=True, context={"request": request}
            )
            return Response({"results": serializer.data})

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
