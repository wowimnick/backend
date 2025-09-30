from django.shortcuts import get_object_or_404
from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
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
from quickstart.models import Booking, BusinessInfo, ClassesMain, Reviews, Schedule
from quickstart.serializers.public.public_business_serializers import (
    PublicBusinessDetailSerializer,
    PublicBusinessInfoSerializer,
    BusinessContactDetailSerializer,
)

logger = logging.getLogger(__name__)


class BusinessPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


class PublicBusinessInfoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides READ-ONLY access to public Business Information.
    Filters to show only active and verified businesses.
    Supports searching and ordering.
    (URL Base: /api/businesses/)
    """

    serializer_class = PublicBusinessInfoSerializer
    lookup_field = "slug"

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicBusinessDetailSerializer
        return PublicBusinessInfoSerializer

    permission_classes = [AllowAny]
    pagination_class = BusinessPagination

    # Add standard filtering/searching capabilities
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "businessName",
        "businessDescription",
        "businessCity",
        "businessState",
    ]
    ordering_fields = [
        "businessName",
        "createdAt",
        "featured",
    ]
    ordering = [
        "-featured",
        "businessName",
    ]

    def get_queryset(self):
        """
        Optimized to pre-fetch related data and filter for active classes
        on the detail view.
        """
        queryset = (
            BusinessInfo.objects.filter(isActive=True, verificationStatus="verified")
            .select_related("partner_tier")
            .order_by("-featured", "businessName")
        )

        if self.action == "retrieve":
            # Define subqueries to calculate min prices and review data for each class
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

            # This is the crucial change. We create a queryset for the prefetch
            # that includes all the necessary annotations for the serializer.
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

            # Use the annotated queryset in the Prefetch object
            active_classes_prefetch = Prefetch(
                "classes",
                queryset=classes_queryset,
                to_attr="active_classes",
            )

            queryset = queryset.prefetch_related(
                active_classes_prefetch,
                Prefetch(
                    "reviews_directly_to_business",
                    queryset=Reviews.objects.select_related("userId").filter(
                        status="approved"
                    ),
                ),
            )

        return queryset

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
