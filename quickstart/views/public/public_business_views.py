from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.db.models import (
    Prefetch,
    Subquery,
    OuterRef,
    DecimalField,
    Count,
    Avg,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal
import logging

# Adjust import paths based on your project structure
from quickstart.models import (
    Booking,
    BusinessInfo,
    ClassesMain,
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

logger = logging.getLogger(__name__)


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

    # --- CACHING IMPLEMENTED ---
    # Cache for 24 hours. Vary on Authorization header because the serializer's
    # 'is_favorited' field for classes depends on the logged-in user.
    @method_decorator(vary_on_headers("Authorization"))
    @method_decorator(cache_page(60 * 60 * 24))
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        # Soonest upcoming instance per class for "Upcoming" cards on business page
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
            avg_rating_subquery = Subquery(
                Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
                .values("classId")
                .annotate(avg_rating=Avg("rating"))
                .values("avg_rating")[:1],
                output_field=DecimalField(max_digits=3, decimal_places=1),
            )
            review_count_subquery = Subquery(
                Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
                .values("classId")
                .annotate(count=Count("reviewId"))
                .values("count")[:1],
            )

            classes_queryset = (
                ClassesMain.objects.filter(status="active")
                .prefetch_related("images", "options")
                .annotate(
                    min_session_price=Coalesce(min_session_price_subquery, None),
                    min_course_price=Coalesce(min_course_price_subquery, None),
                    average_rating=Coalesce(avg_rating_subquery, Value(Decimal("0.0"))),
                    review_count=Coalesce(review_count_subquery, Value(0)),
                )
            )

            active_classes_prefetch = Prefetch(
                "classes", queryset=classes_queryset, to_attr="active_classes"
            )

            # Prefetch google reviews as well
            queryset = queryset.prefetch_related(
                active_classes_prefetch,
                "imported_google_reviews",  # Prefetch google reviews
                Prefetch(
                    "reviews_directly_to_business",
                    queryset=Reviews.objects.select_related("userId").filter(
                        status="approved"
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
