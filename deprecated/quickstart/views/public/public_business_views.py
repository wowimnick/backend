from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from django.core.cache import cache
from django.db.models import (
    Prefetch,
    Subquery,
    OuterRef,
    DecimalField,
    Count,
    Avg,
    Value,
    F,
    Case,
    When,
    IntegerField,
    ExpressionWrapper,
    Q,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal
import logging

# Adjust import paths based on your project structure
from quickstart.models import (
    Booking,
    BusinessInfo,
    BusinessLocation,
    ClassImage,
    ClassesMain,
    MembershipProduct,
    Reviews,
    Schedule,
    ScheduleInstance,
)
from quickstart.serializers.public.public_business_serializers import (
    PublicBusinessDetailSerializer,
    PublicBusinessInfoSerializer,
    BusinessContactDetailSerializer,
    ImportedGoogleReviewSerializer,
)
from quickstart.serializers.public.public_review_serializers import (
    PublicReviewSerializer,
)
from quickstart.views.public.public_class_views import (
    _public_class_options_prefetch_queryset,
)

logger = logging.getLogger(__name__)

BUSINESS_DETAIL_CACHE_PREFIX = "business_detail"
BUSINESS_DETAIL_CACHE_VERSION_PREFIX = "business_detail_version"
BUSINESS_DETAIL_CACHE_TTL = 60 * 60 * 24  # 24 hours


def invalidate_business_detail_cache(slug):
    """
    Invalidate cached business detail API response for this slug.
    Call when the business or any of its classes (e.g. cover image) change,
    so Next.js revalidation gets fresh data from the API.
    """
    if not slug:
        return
    try:
        version = cache.get(f"{BUSINESS_DETAIL_CACHE_VERSION_PREFIX}:{slug}", 0) or 0
        cache.set(f"{BUSINESS_DETAIL_CACHE_VERSION_PREFIX}:{slug}", version + 1, timeout=None)
        logger.info("Invalidated business detail cache for slug=%s (version -> %s)", slug, version + 1)
    except Exception as e:
        logger.warning("Failed to invalidate business detail cache for slug=%s: %s", slug, e)


class BusinessPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


class PublicBusinessInfoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PublicBusinessInfoSerializer
    lookup_field = "slug"
    permission_classes = [AllowAny]
    pagination_class = BusinessPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "businessName",
        "businessDescription",
        "businessCity",
        "businessState",
    ]
    ordering_fields = ["businessName", "createdAt", "featured"]
    ordering = ["-featured", "businessName"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicBusinessDetailSerializer
        return PublicBusinessInfoSerializer

    @action(detail=True, url_path="memberships", methods=["get"])
    def memberships(self, request, slug=None):
        """List active membership products for this business (public)."""
        business = self.get_object()
        products = MembershipProduct.objects.filter(
            business=business, is_active=True
        ).order_by("price")
        data = [
            {
                "id": str(p.id),
                "name": p.name,
                "badge_text": getattr(p, "badge_text", "") or "",
                "description": p.description or "",
                "price": str(p.price),
                "currency": p.currency,
                "billing_interval": p.billing_interval,
                "access_type": p.access_type,
                "credit_allowance": p.credit_allowance,
                "credit_unit": p.credit_unit or "",
            }
            for p in products
        ]
        return Response(data, status=status.HTTP_200_OK)

    # --- CACHING: versioned so we can invalidate when class/business changes ---
    # Authenticated users get fresh data (is_favorited). Anonymous use cache; invalidate on update.
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        slug = instance.slug

        if request.user.is_authenticated:
            # No cache for logged-in users so is_favorited is always correct
            return self._build_business_detail_response(instance, request)

        version = cache.get(f"{BUSINESS_DETAIL_CACHE_VERSION_PREFIX}:{slug}", 0) or 0
        cache_key = f"{BUSINESS_DETAIL_CACHE_PREFIX}:{slug}:v{version}:anon"
        data = cache.get(cache_key)
        if data is not None:
            return Response(data)

        response = self._build_business_detail_response(instance, request)
        try:
            cache.set(cache_key, response.data, timeout=BUSINESS_DETAIL_CACHE_TTL)
        except Exception as e:
            logger.warning("Failed to set business detail cache for slug=%s: %s", slug, e)
        return response

    def _build_business_detail_response(self, instance, request):
        soonest_per_class = {}
        class_ids = [c.pk for c in getattr(instance, "active_classes", [])]
        if class_ids:
            today = timezone.now().date()
            soonest_instances = (
                ScheduleInstance.objects.filter(
                    schedule__option__classId__in=class_ids,
                    date__gte=today,
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
        context = {**self.get_serializer_context(), "soonest_per_class": soonest_per_class}
        serializer = self.get_serializer(instance, context=context)
        return Response(serializer.data)

    def get_queryset(self):
        queryset = (
            BusinessInfo.objects.filter(isActive=True, verificationStatus="verified")
            .select_related("partner_tier")
            .order_by("-featured", "businessName")
        )

        if self.action == "retrieve":
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

            classes_queryset = (
                ClassesMain.objects.filter(status="active")
                .select_related("location_ref", "businessId")
                .prefetch_related(
                    Prefetch(
                        "images",
                        queryset=ClassImage.objects.only(
                            "imageId",
                            "classId_id",
                            "image",
                            "isCover",
                            "createdAt",
                        ).order_by("-isCover", "createdAt"),
                    ),
                    Prefetch(
                        "options",
                        queryset=_public_class_options_prefetch_queryset(),
                    ),
                )
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
                .annotate(
                    review_count=ExpressionWrapper(
                        F("p_count_raw") + F("g_count_raw"),
                        output_field=IntegerField(),
                    ),
                    weighted_sum=ExpressionWrapper(
                        (F("p_rating_raw") * F("p_count_raw"))
                        + (F("g_rating_raw") * F("g_count_raw")),
                        output_field=DecimalField(max_digits=10, decimal_places=2),
                    ),
                )
                .annotate(
                    average_rating=Case(
                        When(review_count=0, then=Value(Decimal("0.0"))),
                        default=ExpressionWrapper(
                            F("weighted_sum") / F("review_count"),
                            output_field=DecimalField(max_digits=3, decimal_places=1),
                        ),
                        output_field=DecimalField(max_digits=3, decimal_places=1),
                    ),
                    min_session_price=Coalesce(min_session_price_subquery, None),
                    min_course_price=Coalesce(min_course_price_subquery, None),
                )
            )

            active_classes_prefetch = Prefetch(
                "classes", queryset=classes_queryset, to_attr="active_classes"
            )

            queryset = queryset.prefetch_related(
                active_classes_prefetch,
                Prefetch(
                    "locations",
                    queryset=BusinessLocation.objects.filter(is_active=True).order_by(
                        "-is_primary", "name"
                    ),
                ),
            )

        return queryset

    @action(
        detail=True, methods=["get"], url_path="reviews", permission_classes=[AllowAny]
    )
    def list_reviews(self, request, slug=None):
        """
        A paginated endpoint to fetch combined platform and Google reviews for a business.
        """
        business = self.get_object()

        # Fetch both review types
        platform_reviews = Reviews.objects.filter(
            businessId=business, status="approved"
        ).select_related("userId")
        google_reviews = business.imported_google_reviews.all()

        # Combine and add metadata for sorting and serialization
        combined_list = [
            {"date": r.createdAt, "data": r, "type": "platform"}
            for r in platform_reviews
        ] + [
            {"date": r.review_date, "data": r, "type": "google"} for r in google_reviews
        ]

        # Sort the combined list by date, newest first
        combined_list.sort(key=lambda x: x["date"], reverse=True)

        # Manually paginate the sorted list
        paginator = PageNumberPagination()
        paginator.page_size = request.query_params.get("page_size", 10)
        paginated_list = paginator.paginate_queryset(combined_list, request, view=self)

        # Serialize the data for the current page
        serialized_data = []
        for item in paginated_list:
            if item["type"] == "platform":
                serialized_data.append(PublicReviewSerializer(item["data"]).data)
            else:
                serialized_data.append(
                    ImportedGoogleReviewSerializer(item["data"]).data
                )

        return paginator.get_paginated_response(serialized_data)

    @action(detail=True, methods=["get"], permission_classes=[IsAuthenticated])
    def contact_details(self, request, slug=None):
        """
        An authenticated action to reveal business contact details
        ONLY to users who have a confirmed or completed booking.
        """
        user = request.user
        business = self.get_object()

        has_booking = Booking.objects.filter(
            user=user,
            schedule_instance__schedule__option__classId__businessId=business,
            status__in=["confirmed", "completed"],
        ).exists()

        if not has_booking:
            return Response(
                {
                    "detail": "You must have a booking with this business to view contact details."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = BusinessContactDetailSerializer(business)
        return Response(serializer.data, status=status.HTTP_200_OK)
