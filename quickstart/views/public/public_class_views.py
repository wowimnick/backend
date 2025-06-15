import math
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.db.models import (
    Q, Avg, Count, Min, Max, Value, F, Subquery, OuterRef, DecimalField,
    IntegerField, Sum, Case, When, ExpressionWrapper, FloatField, Func, Prefetch
)
from django.db.models.functions import (
    Coalesce, Power, Log, Now, Extract, Sin, Cos, Radians, Cast,
    StrIndex, Substr, Length
)
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
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

from ...models import (
    ClassesMain,
    ClassOption,
    Reviews,
    Booking,
    Schedule,
    ScheduleInstance,
)
from ...serializers import (
    PublicClassSerializer,
    ScheduleSerializer,
)
from ..utils import haversine_distance

logger = logging.getLogger(__name__)

PHOTON_API_URL = "https://photon.komoot.io/api/"
DEFAULT_SEARCH_RADIUS_KM = 50

# --- NEW: Relevance Scoring Weights (Tune these values based on business goals) ---
W_FEATURED = 1.5  # Multiplier for featured businesses
W_QUALITY = 1.0  # Base weight for quality score (description, images)
W_RATING = 0.8  # Weight for average rating
W_REVIEW_COUNT = 0.5  # Weight for number of reviews (log-scaled)
W_NEWNESS = 0.7  # Weight for how new a class is (decaying)

# --- NEW: Relevance Score Normalization/Tuning Constants ---
QUALITY_SCORE_MAX_DESCRIPTION_LEN = 1000  # Optimal description length for max score
QUALITY_SCORE_MAX_IMAGES = 5  # Number of images to reach max score
RECENCY_HALFLIFE_DAYS = 90  # A class's "newness" boost drops by 50% every 90 days
REVIEW_COUNT_FOR_MAX_SCORE = 50  # Number of reviews to get the max review count score


def geocode_location_text_backend(location_text):
    if not location_text:
        return None
    try:
        params = {"q": quote(location_text), "limit": 1}
        headers = {"User-Agent": "ClasseasilyApp/1.0 (Python Requests)"}
        response = requests.get(
            PHOTON_API_URL, params=params, headers=headers, timeout=5
        )
        response.raise_for_status()
        data = response.json()
        if data and data.get("features") and len(data["features"]) > 0:
            feature = data["features"][0]
            lon, lat = feature["geometry"]["coordinates"]
            return float(lat), float(lon) # Only need lat/lon here now
    except requests.RequestException as e:
        logger.error(f"Backend geocoding HTTP error for '{location_text}': {e}")
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Error parsing geocoding response for '{location_text}': {e}")
    return None

class Acos(Func):
    function = 'ACOS'

class PublicClassViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = PublicClassSerializer

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
    MIN_PRICE_SUBQUERY = Subquery(
        Schedule.objects.filter(
            option__classId=OuterRef("pk"),
        )
        .filter(
            Q(option__booking_type="Full Course", end_date__gte=timezone.now().date())
            | Q(option__booking_type="Single Session", date__gte=timezone.now().date())
        )
        .order_by("price")
        .values("price")[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    MAX_PRICE_SUBQUERY = Subquery(
        Schedule.objects.filter(
            option__classId=OuterRef("pk"),
        )
        .filter(
            Q(option__booking_type="Full Course", end_date__gte=timezone.now().date())
            | Q(option__booking_type="Single Session", date__gte=timezone.now().date())
        )
        .order_by("-price")
        .values("price")[:1],
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )

    def get_queryset(self):
        return (
            ClassesMain.objects.select_related("businessId", "category", "subcategory")
            .prefetch_related(
                "images",
                Prefetch(
                    "options__schedules",
                    queryset=Schedule.objects.filter(
                        Q(option__booking_type="Full Course", end_date__gte=timezone.now().date()) |
                        Q(option__booking_type="Single Session", date__gte=timezone.now().date())
                    ),
                ),
            )
            .filter(
                status="active",
                businessId__isActive=True,
                businessId__verificationStatus="verified",
            )
            .annotate(
                # Annotate base metrics here for reusability
                average_rating=Coalesce(self.AVERAGE_RATING_SUBQUERY, Value(Decimal("0.0"))),
                review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
                min_price=Coalesce(self.MIN_PRICE_SUBQUERY, None),
                image_count=Count('images', distinct=True) # NEW: For quality score
            )
            .distinct()
        )

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
            # --- Get and Process Query Parameters ---
            req_lat_str = request.query_params.get("lat")
            req_lng_str = request.query_params.get("lng")
            req_radius_km_str = request.query_params.get("radius")
            location_search_text = request.query_params.get("location_search")
            
            # --- Location Processing ---
            search_lat, search_lng = None, None
            if req_lat_str and req_lng_str:
                try:
                    search_lat, search_lng = float(req_lat_str), float(req_lng_str)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid geo params: lat='{req_lat_str}', lng='{req_lng_str}'")
            elif location_search_text:
                geocoded_result = geocode_location_text_backend(location_search_text)
                if geocoded_result:
                    search_lat, search_lng = geocoded_result

            # --- Base Queryset ---
            queryset = self.get_queryset()

            # --- Category Filter ---
            category_key = request.query_params.get("category_key")
            subcategory_key = request.query_params.get("subcategory_key")
            if category_key and category_key.lower() != "all":
                queryset = queryset.filter(category__key=category_key)
                if subcategory_key:
                    queryset = queryset.filter(subcategory__key=subcategory_key)
            
            # --- Price Filter ---
            price_max_str = request.query_params.get("price_max")
            if price_max_str:
                try:
                    queryset = queryset.filter(
                        Q(min_price__lte=Decimal(price_max_str)) | Q(min_price__isnull=True)
                    )
                except InvalidOperation:
                    logger.warning(f"Invalid price_max: {price_max_str}")

            # --- Date and Availability Filters ---
            req_date_str = request.query_params.get("date")
            req_participants_str = request.query_params.get("participants")
            
            instance_filters = Q(options__schedules__instances__status="scheduled")
            if req_date_str:
                try:
                    target_date = datetime.strptime(req_date_str, "%Y-%m-%d").date()
                    instance_filters &= Q(options__schedules__instances__date=target_date)
                except ValueError:
                    instance_filters &= Q(options__schedules__instances__date__gte=timezone.now().date())
            else:
                instance_filters &= Q(options__schedules__instances__date__gte=timezone.now().date())
            
            if req_participants_str and req_participants_str.isdigit() and int(req_participants_str) > 0:
                instance_filters &= Q(options__schedules__instances__max_participants__gte=int(req_participants_str))
                
            queryset = queryset.filter(instance_filters).distinct()

            # --- Haversine Distance Calculation (Parsing String in DB) ---
            if search_lat is not None and search_lng is not None:
                # Exclude rows with invalid coordinate formats to prevent DB errors
                queryset = queryset.exclude(Q(coordinates__isnull=True) | Q(coordinates__exact='') | ~Q(coordinates__contains=','))

                # Use database functions to split the string and cast to float
                comma_pos = StrIndex(F('coordinates'), Value(','))
                db_lat = Cast(Substr(F('coordinates'), 1, comma_pos - 1), output_field=FloatField())
                db_lng = Cast(Substr(F('coordinates'), comma_pos + 1), output_field=FloatField())

                lat_r = Radians(db_lat)
                lng_r = Radians(db_lng)
                search_lat_r = Radians(Value(search_lat))
                search_lng_r = Radians(Value(search_lng))

                d_lng = lng_r - search_lng_r
                d_lat = lat_r - search_lat_r
                a = (Power(Sin(d_lat / 2), 2) + Cos(search_lat_r) * Cos(lat_r) * Power(Sin(d_lng / 2), 2))
                c = 2 * Acos(Power(a, 0.5))
                distance_expr = ExpressionWrapper(6371 * c, output_field=FloatField())
                
                queryset = queryset.annotate(distance=distance_expr)
                
                if req_radius_km_str and req_radius_km_str.replace('.', '', 1).isdigit() and float(req_radius_km_str) > 0:
                    queryset = queryset.filter(distance__lte=float(req_radius_km_str))

            # --- Relevance Score Calculation (DB Level) ---
            # Get days since creation (epoch seconds / seconds in a day)
            days_old = Extract(Now() - F('createdAt'), 'epoch') / Value(86400.0)

            # Quality Score (0-1): Combination of description length and image count
            quality_score = ExpressionWrapper(
                (
                    (Log(10, Length('description') + 1) / Log(10, Value(QUALITY_SCORE_MAX_DESCRIPTION_LEN + 1))) +
                    (Log(10, F('image_count') + 1) / Log(10, Value(QUALITY_SCORE_MAX_IMAGES + 1)))
                ) / 2.0,
                output_field=FloatField()
            )
            
            rating_score = ExpressionWrapper(
                F('average_rating') / Value(5.0),
                output_field=FloatField()
            )
            
            review_count_score = ExpressionWrapper(
                Log(10, F('review_count') + 1) / Log(10, Value(REVIEW_COUNT_FOR_MAX_SCORE + 1)),
                output_field=FloatField()
            )
            
            newness_score = ExpressionWrapper(
                Power(2, -days_old / Value(RECENCY_HALFLIFE_DAYS)),
                output_field=FloatField()
            )

            featured_multiplier = Case(When(businessId__featured=True, then=Value(W_FEATURED)), default=Value(1.0), output_field=FloatField())

            # Combine all weighted scores into a final relevance score
            relevance_score = ExpressionWrapper(
                (
                    (Value(W_QUALITY) * quality_score) +
                    (Value(W_RATING) * rating_score) +
                    (Value(W_REVIEW_COUNT) * review_count_score) +
                    (Value(W_NEWNESS) * newness_score)
                ) * featured_multiplier,
                output_field=FloatField()
            )

            queryset = queryset.annotate(relevance_score=relevance_score)

            # --- Sorting (DB Level) ---
            sort_by = request.query_params.get("sort_by", "relevance")
            if sort_by == "relevance":
                queryset = queryset.order_by('-relevance_score', '-createdAt')
            elif sort_by == "distance" and search_lat is not None:
                # Ensure classes without coordinates are last
                queryset = queryset.order_by(F('distance').asc(nulls_last=True))
            elif sort_by == "price_asc":
                queryset = queryset.order_by(F('min_price').asc(nulls_last=True))
            elif sort_by == "price_desc":
                queryset = queryset.order_by(F('min_price').desc(nulls_first=True))
            elif sort_by == "rating":
                queryset = queryset.order_by('-average_rating', '-review_count')
            elif sort_by == "newest":
                queryset = queryset.order_by('-createdAt')

            # --- Pagination & Serialization ---
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)

            # Fallback if pagination is not used for some reason
            serializer = self.get_serializer(queryset, many=True, context={'request': request})
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
    def availability(self, request):
        option_id_str = request.query_params.get("option_id")
        start_date_str = request.query_params.get("start_date")
        end_date_str = request.query_params.get("end_date")

        if not (option_id_str and option_id_str.isdigit()):
            return Response(
                {"error": "Valid 'option_id' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not (start_date_str and end_date_str):
            return Response(
                {"error": "'start_date' and 'end_date' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        option_id = int(option_id_str)
        try:
            start_date_obj = timezone.datetime.strptime(
                start_date_str, "%Y-%m-%d"
            ).date()
            end_date_obj = timezone.datetime.strptime(end_date_str, "%Y-%m-%d").date()
            if start_date_obj > end_date_obj:
                return Response(
                    {"error": "Start date cannot be after end date."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except ValueError:
            return Response(
                {"error": "Invalid date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ClassOption.objects.select_related("classId__businessId").get(
                optionId=option_id,
                classId__status="active",
                classId__businessId__isActive=True,
                classId__businessId__verificationStatus="verified",
            )
        except ClassOption.DoesNotExist:
            return Response(
                {"error": "Requested class option not found or is not available."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            instances = (
                ScheduleInstance.objects.filter(
                    schedule__option_id=option_id,
                    date__range=(start_date_obj, end_date_obj),
                    status="scheduled",
                )
                .annotate(
                    current_bookings_count=Coalesce(
                        Sum(
                            "bookings__participants",
                            filter=Q(bookings__status="confirmed"),
                        ),
                        0,
                        output_field=IntegerField(),
                    )
                )
                .select_related("schedule")
                .order_by("date", "time")
            )

            availability_data = {}
            for instance in instances:
                date_key = instance.date.isoformat()
                if date_key not in availability_data:
                    availability_data[date_key] = []

                available_spots = (
                    instance.max_participants - instance.current_bookings_count
                )
                if available_spots > 0:
                    availability_data[date_key].append(
                        {
                            "instance_id": instance.pk,
                            "time": instance.time.strftime("%H:%M"),
                            "duration": instance.duration,
                            "available_spots": available_spots,
                            "price": str(instance.price),
                        }
                    )
                if date_key in availability_data and not availability_data[date_key]:
                    del availability_data[date_key]

            return Response(availability_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(
                f"Error fetching availability for option {option_id}: {e}",
                exc_info=True,
            )
            return Response(
                {"error": "An error occurred while fetching availability."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
