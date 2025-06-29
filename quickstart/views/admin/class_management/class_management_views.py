from datetime import timedelta
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated, BasePermission
from django.db import transaction
from django.db.models import (
    Q,
    Min,
    Max,
    Avg,
    Count,
    F,
    Value,
    Subquery,
    Exists,
    Prefetch,
    OuterRef,
    DecimalField,
    FloatField,
    ExpressionWrapper,
    fields,
    Sum,
)
from decimal import Decimal
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.http import HttpResponse  # For CSV export
import csv  # For CSV export
import logging

from quickstart.models import (
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassOption,
    Payment,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    BusinessInfo,
    VerificationRequest,  # Ensure BusinessInfo is imported if needed for hierarchy checks
)
from quickstart.serializers.admin.class_management.class_management_serializers import (
    AdminClassSerializer,
    AdminClassDetailSerializer,
    # AdminClassCreateSerializer, # Keep if used, commented out if not needed in this file context
    AdminClassCategorySerializer,
    AdminReviewSerializer,
    ReassignmentSerializer,
    SubcategorySerializer,
)

logger = logging.getLogger(__name__)

# --- Custom Permission Classes (Example - Adapt as needed) ---


class CanAccessClassAdmin(BasePermission):
    message = "You do not have permission to access class administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        # Check for a specific permission related to class admin access
        return request.user.has_perm(
            "quickstart.access_class_admin"
        )  # Make sure this permission exists


class CanAccessCategoryAdmin(BasePermission):
    message = "You do not have permission to access category administration."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm(
            "quickstart.access_category_admin"
        )  # Make sure this permission exists


class CanAccessReviewAdmin(BasePermission):
    message = "You do not have permission to access review moderation."

    def has_permission(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or not request.user.is_active
        ):
            return False
        return request.user.has_perm(
            "quickstart.access_review_admin"
        )  # Make sure this permission exists


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminClassViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing classes
    """

    permission_classes = [IsAuthenticated, CanAccessClassAdmin]
    pagination_class = StandardResultsSetPagination

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminClassDetailSerializer
        return AdminClassSerializer

    http_method_names = [
        "get",
        "post",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
    ]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "businessId__businessName", "category__name", "location"]
    ordering_fields = [
        "title",
        "createdAt",
        "average_rating",
        "review_count",
        "active_schedules_count",
        "business_name",
        "status",
        "min_price",
        "platform_revenue",  # Added for sorting
    ]
    ordering = ["-createdAt"]

    def get_queryset(self):
        """
        Get queryset for admin class views, annotated with necessary metrics.
        """
        if not self.request.user.has_perm("quickstart.view_classesmain"):
            logger.warning(
                f"User {self.request.user.email} denied access to list classes (missing view_classesmain perm)."
            )
            return ClassesMain.objects.none()

        try:
            queryset = (
                ClassesMain.objects.select_related(
                    "businessId", "category", "subcategory", "businessId__owner"
                )
                .prefetch_related(
                    Prefetch(
                        "options__schedules",
                        queryset=Schedule.objects.filter(price__isnull=False),
                    ),
                    "options__schedules__instances",
                    "reviews",
                    "images",
                )
                .distinct()
            )

            # --- Annotations ---
            approved_rating_subquery = Subquery(
                Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
                .values("classId")
                .annotate(avg=Avg("rating"))
                .values("avg"),
                output_field=FloatField(),
            )
            approved_review_count_subquery = Subquery(
                Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
                .values("classId")
                .annotate(c=Count("pk"))
                .values("c"),
                output_field=Count("pk").output_field,
            )

            min_price_subquery = Subquery(
                Schedule.objects.filter(
                    option__classId=OuterRef("pk"), price__isnull=False
                )
                .order_by("price")
                .values("price")[:1],
                output_field=DecimalField(),
            )
            max_price_subquery = Subquery(
                Schedule.objects.filter(
                    option__classId=OuterRef("pk"), price__isnull=False
                )
                .order_by("-price")
                .values("price")[:1],
                output_field=DecimalField(),
            )

            active_instances_subquery = Subquery(
                ScheduleInstance.objects.filter(
                    schedule__option__classId=OuterRef("pk"),
                    date__gte=timezone.now().date(),
                    status="scheduled",
                )
                .values("schedule__option__classId")
                .annotate(c=Count("pk", distinct=True))
                .values("c"),
                output_field=Count("pk").output_field,
            )

            platform_revenue_subquery = Subquery(
                Payment.objects.filter(
                    booking__schedule_instance__schedule__option__classId=OuterRef(
                        "pk"
                    ),
                    status="succeeded",
                )
                .values(
                    "booking__schedule_instance__schedule__option__classId"
                )  # Group by class
                .annotate(total_fees=Sum("service_fee_amount"))
                .values("total_fees")[:1],
                output_field=DecimalField(),
            )

            queryset = queryset.annotate(
                business_name=F("businessId__businessName"),
                business_featured=F("businessId__featured"),
                average_rating=Coalesce(
                    approved_rating_subquery, Value(0.0), output_field=FloatField()
                ),
                review_count=Coalesce(
                    approved_review_count_subquery,
                    Value(0),
                    output_field=Count("pk").output_field,
                ),
                min_price=Coalesce(
                    min_price_subquery,
                    None,
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                ),
                max_price=Coalesce(
                    max_price_subquery,
                    None,
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                ),
                active_schedules_count=Coalesce(
                    active_instances_subquery,
                    Value(0),
                    output_field=Count("pk").output_field,
                ),
                platform_revenue=Coalesce(
                    platform_revenue_subquery,
                    Value(Decimal("0.00")),
                    output_field=DecimalField(),
                ),
            )

            # --- Filtering Logic ---
            category_id = self.request.query_params.get("category_id")
            if category_id and category_id.isdigit():
                queryset = queryset.filter(category_id=category_id)

            status_filter = self.request.query_params.get("status")
            if status_filter and status_filter != "all":
                valid_statuses = [choice[0] for choice in ClassesMain.STATUS_CHOICES]
                if status_filter in valid_statuses:
                    queryset = queryset.filter(status=status_filter)

            featured = self.request.query_params.get("featured")
            if featured is not None:
                is_featured = str(featured).lower() in ["true", "1", "yes"]
                queryset = queryset.filter(business_featured=is_featured)

            return queryset

        except Exception as e:
            logger.error(
                f"Error in AdminClassViewSet.get_queryset: {str(e)}", exc_info=True
            )
            return ClassesMain.objects.none()

    # --- Standard Actions Overridden for Permissions ---

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
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

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_classesmain"):
            self.permission_denied(request, message="You cannot view class details.")
        instance = self.get_object()
        serializer = self.get_serializer(instance, context={"request": request})
        return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_classesmain"):
            self.permission_denied(request, message="You cannot update class details.")

        instance = self.get_object()
        serializer = self.get_serializer(
            instance, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        logger.info(
            f"Class '{instance.title}' (ID: {instance.pk}) partially updated by Admin {request.user.email}"
        )

        detail_serializer = AdminClassDetailSerializer(
            instance, context={"request": request}
        )
        return Response(detail_serializer.data)

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_classesmain"):
            self.permission_denied(request, message="You cannot delete classes.")

        instance = self.get_object()
        class_title = instance.title
        logger.warning(
            f"Class '{class_title}' (ID: {instance.pk}) deleted by Admin {request.user.email}"
        )

        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- Custom Actions ---

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """
        Provides high-level statistics for the Class Management dashboard,
        including class, category, and subcategory overviews.
        """
        if not request.user.has_perm("quickstart.view_class_analytics"):
            self.permission_denied(request, message="You cannot view class analytics.")

        try:
            # --- Class Stats ---
            # Filter for active classes now includes checking the business status
            active_classes_count = ClassesMain.objects.filter(
                status="active", businessId__isActive=True
            ).count()

            base_qs = ClassesMain.objects.all()
            total_classes = base_qs.count()
            featured_classes_count = base_qs.filter(businessId__featured=True).count()
            status_counts_qs = (
                base_qs.values("status")
                .annotate(count=Count("classId"))
                .order_by("status")
            )
            status_counts = {item["status"]: item["count"] for item in status_counts_qs}

            # --- Review Stats (Aggregated) ---
            avg_rating_result = Reviews.objects.filter(status="approved").aggregate(
                avg=Coalesce(Avg("rating"), Value(0.0))
            )
            avg_rating = avg_rating_result["avg"]

            # Define the filter to be reused
            active_class_filter = Q(
                classes_in_category__status="active",
                classes_in_category__businessId__isActive=True,
            )

            # --- Category Stats ---
            category_counts_qs = (
                ClassCategory.objects.annotate(
                    class_count=Count(
                        "classes_in_category", filter=active_class_filter, distinct=True
                    )
                )
                .values("id", "name", "key", "color", "class_count")
                .order_by("-class_count")
            )
            total_categories = ClassCategory.objects.count()
            total_subcategories = ClassSubcategory.objects.count()

            # Define the filter for subcategories to be reused
            active_subclass_filter = Q(
                classes_in_subcategory__status="active",
                classes_in_subcategory__businessId__isActive=True,
            )

            # --- Subcategory Stats ---
            subcategory_counts_qs = (
                ClassSubcategory.objects.select_related("category")
                .annotate(
                    class_count=Count(
                        "classes_in_subcategory",
                        filter=active_subclass_filter,
                        distinct=True,
                    )
                )
                .filter(class_count__gt=0)
                .values(
                    "id",
                    "name",
                    "key",
                    "category_id",
                    "category__color",
                    "class_count",
                )
                .order_by("-class_count")
            )

            # --- Top 5 Performers (Bookings & Ratings) ---
            # (No changes needed here as they are based on bookings/ratings, not just "active" status)
            popular_classes_qs = (
                ClassesMain.objects.annotate(
                    bookings_count=Count(
                        "options__schedules__instances__bookings",
                        filter=Q(
                            options__schedules__instances__bookings__status__in=[
                                "confirmed",
                                "completed",
                            ]
                        ),
                        distinct=True,
                    )
                )
                .filter(bookings_count__gt=0)
                .order_by("-bookings_count")[:5]
            )
            popular_classes_data = list(
                popular_classes_qs.values("classId", "title", "bookings_count")
            )

            top_rated_classes_qs = (
                ClassesMain.objects.annotate(
                    average_rating=Coalesce(
                        Avg("reviews__rating", filter=Q(reviews__status="approved")),
                        Value(0.0),
                        output_field=FloatField(),
                    ),
                )
                .filter(average_rating__gt=0)
                .order_by("-average_rating")[:5]
            )
            top_rated_classes_data = list(
                top_rated_classes_qs.values("classId", "title", "average_rating")
            )

            return Response(
                {
                    "totalClasses": total_classes,
                    "activeClasses": active_classes_count,  # This count is now accurate
                    "averageRating": round(avg_rating, 1),
                    "totalCategories": total_categories,
                    "totalSubcategories": total_subcategories,
                    "categoryClassCounts": list(category_counts_qs),
                    "subcategoryClassCounts": list(subcategory_counts_qs),
                    "featuredClasses": featured_classes_count,
                    "statusCounts": status_counts,
                    "popularClasses": popular_classes_data,
                    "topRatedClasses": top_rated_classes_data,
                }
            )
        except Exception as e:
            logger.error(f"Error generating class analytics: {e}", exc_info=True)
            return Response(
                {"error": "Could not generate analytics"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"])
    def export(self, request):
        if not request.user.has_perm("quickstart.export_class_data"):
            self.permission_denied(request, message="You cannot export class data.")

        try:
            queryset = self.filter_queryset(self.get_queryset())
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = (
                'attachment; filename="classes_export.csv"'
            )
            writer = csv.writer(response)

            headers = [
                "Class ID",
                "Title",
                "Business Name",
                "Category",
                "Subcategory",
                "Status",
                "Average Rating",
                "Review Count",
                "Active Schedules Count",
                "Min Price",
                "Max Price",
                "Location",
                "Business Featured",
                "Created At",
            ]
            writer.writerow(headers)

            class_data = queryset.values_list(
                "classId",
                "title",
                "business_name",
                "category__name",
                "subcategory__name",
                "status",
                "average_rating",
                "review_count",
                "active_schedules_count",
                "min_price",
                "max_price",
                "location",
                "business_featured",
                "createdAt",
            )

            for data_tuple in class_data:
                (
                    classId,
                    title,
                    business_name,
                    category_name,
                    subcategory_name,
                    status_val,  # Renamed status to status_val
                    avg_rating,
                    review_count,
                    active_schedules_count,
                    min_price,
                    max_price,
                    location,
                    business_featured,
                    createdAt,
                ) = data_tuple

                writer.writerow(
                    [
                        classId,
                        title,
                        business_name,
                        category_name or "N/A",
                        subcategory_name or "N/A",
                        status_val,  # Use status_val
                        round(avg_rating or 0.0, 1),
                        review_count or 0,
                        active_schedules_count or 0,
                        f"{min_price:.2f}" if min_price is not None else "N/A",
                        f"{max_price:.2f}" if max_price is not None else "N/A",
                        location,
                        "Yes" if business_featured else "No",
                        createdAt.strftime("%Y-%m-%d %H:%M:%S") if createdAt else "",
                    ]
                )
            return response
        except Exception as e:
            logger.error(f"Error exporting class data: {e}", exc_info=True)
            return Response(
                {"error": "Failed to export class data"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["patch"], url_path="update_class_status")
    def update_class_status(self, request, pk=None):
        if not request.user.has_perm("quickstart.change_class_status"):
            self.permission_denied(request, message="You cannot change class status.")

        class_instance = self.get_object()

        new_status = request.data.get("status")
        reason = request.data.get("reason", "")

        valid_statuses = [choice[0] for choice in ClassesMain.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response(
                {
                    "error": f'Invalid status value. Must be one of: {", ".join(valid_statuses)}'
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = class_instance.status
        if old_status == new_status:
            serializer = self.get_serializer(class_instance)
            return Response(serializer.data)

        class_instance.status = new_status
        class_instance.save(update_fields=["status"])

        logger.info(
            f"Class '{class_instance.title}' (ID: {pk}) status changed from {old_status} to {new_status} by Admin {request.user.email}. Reason: {reason}"
        )

        serializer = self.get_serializer(class_instance)
        return Response(serializer.data)


# --- AdminCategoryViewSet ---
class AdminCategoryViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing class categories
    """

    permission_classes = [IsAuthenticated, CanAccessCategoryAdmin]
    serializer_class = AdminClassCategorySerializer
    queryset = ClassCategory.objects.all().order_by("name")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "key"]

    def get_queryset(self):
        # Using get_queryset to handle annotations centrally
        queryset = super().get_queryset()

        # Prefetch subcategories with their own class counts
        subcat_queryset = ClassSubcategory.objects.annotate(
            class_count=Count("classes_in_subcategory", distinct=True)
        )

        # Annotate categories with their class counts
        annotated_queryset = queryset.annotate(
            active_classes=Count(
                "classes_in_category",
                filter=Q(classes_in_category__status="active"),
                distinct=True,
            ),
            class_count=Count("classes_in_category", distinct=True),
        ).prefetch_related(Prefetch("subcategories", queryset=subcat_queryset))

        return annotated_queryset

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.add_classcategory"):
            self.permission_denied(request, message="You cannot create categories.")
        response = super().create(request, *args, **kwargs)
        if response.status_code == status.HTTP_201_CREATED:
            logger.info(
                f"Category '{response.data.get('name')}' created by Admin {request.user.email}"
            )
        return response

    def update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_classcategory"):
            self.permission_denied(request, message="You cannot update categories.")
        instance = self.get_object()
        old_name = instance.name
        response = super().update(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            logger.info(
                f"Category '{old_name}' (ID: {instance.pk}) updated by Admin {request.user.email}"
            )
        return response

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_classcategory"):
            self.permission_denied(request, message="You cannot delete categories.")

        instance = self.get_object()

        if instance.classes_in_category.exists():
            return Response(
                {
                    "error": "This category is in use. Please use the reassignment workflow.",
                    "code": "reassignment_required",
                    "class_count": instance.classes_in_category.count(),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        category_name = instance.name
        instance.delete()
        logger.warning(
            f"Category '{category_name}' (ID: {instance.pk}) with no classes deleted by Admin {request.user.email}"
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_classcategory"):
            self.permission_denied(request, message="You cannot view categories.")

        queryset = self.filter_queryset(self.get_queryset())

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

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_classcategory"):
            self.permission_denied(request, message="You cannot view category details.")
        instance = self.get_object()
        serializer = self.get_serializer(instance, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="delete-with-reassignment")
    @transaction.atomic
    def delete_with_reassignment(self, request, pk=None):
        if not request.user.has_perm("quickstart.delete_classcategory"):
            self.permission_denied(
                request, message="You do not have permission to perform this action."
            )

        category_to_delete = self.get_object()
        serializer = ReassignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_category_id = serializer.validated_data["new_id"]

        if category_to_delete.id == new_category_id:
            return Response(
                {"error": "Cannot reassign to the same category."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            new_category = ClassCategory.objects.get(pk=new_category_id)
        except ClassCategory.DoesNotExist:
            return Response(
                {"error": "The selected new category does not exist."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # MODIFIED: When reassigning a category, the subcategory MUST be cleared.
        updated_count = ClassesMain.objects.filter(category=category_to_delete).update(
            category=new_category, subcategory=None
        )

        logger.info(
            f"{updated_count} classes reassigned from Category '{category_to_delete.name}' to '{new_category.name}' and subcategories cleared."
        )

        # Now, delete the old category (this will cascade to its subcategories)
        category_name = category_to_delete.name
        category_to_delete.delete()

        logger.warning(
            f"Category '{category_name}' (ID: {pk}) deleted after reassigning classes by Admin {request.user.email}."
        )

        return Response(
            {
                "detail": f"Successfully reassigned {updated_count} classes and deleted category '{category_name}'."
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="subcategories")
    def add_subcategory(self, request, pk=None):
        if not request.user.has_perm("quickstart.add_classsubcategory"):
            self.permission_denied(request, message="You cannot add subcategories.")

        category = self.get_object()
        serializer = SubcategorySerializer(
            data=request.data, context={"request": request}
        )
        if serializer.is_valid():
            name = serializer.validated_data.get("name")
            key = serializer.validated_data.get("key")

            if not key:
                from django.utils.text import slugify

                key = slugify(name)
                counter = 1
                original_key = key
                while ClassSubcategory.objects.filter(
                    category=category, key=key
                ).exists():
                    key = f"{original_key}-{counter}"
                    counter += 1
                serializer.validated_data["key"] = key

            if ClassSubcategory.objects.filter(category=category, key=key).exists():
                return Response(
                    {
                        "key": [
                            f"Subcategory with key '{key}' already exists for this category."
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if ClassSubcategory.objects.filter(category=category, name=name).exists():
                return Response(
                    {
                        "name": [
                            f"Subcategory with name '{name}' already exists for this category."
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            instance = serializer.save(category=category)
            logger.info(
                f"Subcategory '{instance.name}' added to Category '{category.name}' by Admin {request.user.email}"
            )

            return Response(
                SubcategorySerializer(instance).data, status=status.HTTP_201_CREATED
            )
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(
        detail=True,
        methods=["patch", "delete", "post"],
        url_path="subcategories/(?P<subcategory_pk>[^/.]+)",
    )
    def update_or_delete_subcategory(self, request, pk=None, subcategory_pk=None):
        if request.method == "POST":
            return self.delete_subcategory_with_reassignment(
                request, pk, subcategory_pk
            )

        try:
            category = self.get_object()
            subcategory = ClassSubcategory.objects.get(
                pk=subcategory_pk, category=category
            )
        except ClassSubcategory.DoesNotExist:
            return Response(
                {"detail": "Subcategory not found for this category."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.method == "PATCH":
            if not request.user.has_perm("quickstart.change_classsubcategory"):
                self.permission_denied(
                    request, message="You cannot edit subcategories."
                )

            serializer = SubcategorySerializer(
                instance=subcategory, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            logger.info(
                f"Subcategory '{subcategory.name}' (ID: {subcategory_pk}) updated by Admin {request.user.email}"
            )
            return Response(serializer.data, status=status.HTTP_200_OK)

        if request.method == "DELETE":
            if not request.user.has_perm("quickstart.delete_classsubcategory"):
                self.permission_denied(
                    request, message="You cannot delete subcategories."
                )

            if subcategory.classes_in_subcategory.exists():
                return Response(
                    {
                        "error": "This subcategory is in use. Please use the reassignment workflow.",
                        "code": "reassignment_required",
                        "class_count": subcategory.classes_in_subcategory.count(),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            subcategory_name = subcategory.name
            subcategory.delete()
            logger.warning(
                f"Subcategory '{subcategory_name}' (ID: {subcategory_pk}) from Category '{category.name}' deleted by Admin {request.user.email}"
            )
            return Response(status=status.HTTP_204_NO_CONTENT)

    @transaction.atomic
    def delete_subcategory_with_reassignment(
        self, request, category_pk, subcategory_pk
    ):
        if not request.user.has_perm("quickstart.delete_classsubcategory"):
            self.permission_denied(
                request, message="You do not have permission to perform this action."
            )

        serializer = ReassignmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_subcategory_id = serializer.validated_data["new_id"]

        try:
            category = ClassCategory.objects.get(pk=category_pk)
            subcategory_to_delete = ClassSubcategory.objects.get(
                pk=subcategory_pk, category=category
            )
            new_subcategory = ClassSubcategory.objects.get(
                pk=new_subcategory_id, category=category
            )
        except (ClassCategory.DoesNotExist, ClassSubcategory.DoesNotExist):
            return Response(
                {"error": "Invalid category or subcategory ID."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if subcategory_to_delete.id == new_subcategory.id:
            return Response(
                {"error": "Cannot reassign to the same subcategory."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated_count = ClassesMain.objects.filter(
            subcategory=subcategory_to_delete
        ).update(subcategory=new_subcategory)

        logger.info(
            f"{updated_count} classes reassigned from Subcategory '{subcategory_to_delete.name}' to '{new_subcategory.name}'."
        )

        subcategory_name = subcategory_to_delete.name
        subcategory_to_delete.delete()

        logger.warning(
            f"Subcategory '{subcategory_name}' deleted after reassigning classes by Admin {request.user.email}."
        )

        return Response(
            {
                "detail": f"Successfully reassigned {updated_count} classes and deleted subcategory '{subcategory_name}'."
            },
            status=status.HTTP_200_OK,
        )


class AdminReviewViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, CanAccessReviewAdmin]
    serializer_class = AdminReviewSerializer
    pagination_class = StandardResultsSetPagination
    queryset = (
        Reviews.objects.select_related(
            "userId", "userId__role", "classId", "classId__businessId", "businessId"
        )
        .prefetch_related("userId__role__permissions")
        .order_by("-createdAt")
    )
    http_method_names = ["get", "patch", "delete", "head", "options", "post"]

    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "comment",
        "userId__email",
        "userId__first_name",
        "classId__title",
        "businessId__businessName",
        "report_reason",
    ]
    ordering_fields = ["createdAt", "rating", "status", "reported"]

    def get_queryset(self):
        if not self.request.user.has_perm("quickstart.view_reviews"):
            self.permission_denied(self.request, message="You cannot view reviews.")

        queryset = super().get_queryset()

        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter != "all":
            valid_statuses = [
                choice[0] for choice in Reviews._meta.get_field("status").choices
            ]
            if status_filter in valid_statuses:
                queryset = queryset.filter(status=status_filter)

        reported = self.request.query_params.get("reported")
        if reported is not None:
            is_reported = str(reported).lower() in ["true", "1", "yes"]
            queryset = queryset.filter(reported=is_reported)

        rating_filter = self.request.query_params.get("rating")
        if rating_filter and rating_filter.isdigit():
            rating_val = int(rating_filter)
            if 1 <= rating_val <= 5:
                queryset = queryset.filter(rating=rating_val)
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
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

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, context={"request": request})
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """Provides statistics for the Review Moderation dashboard."""
        if not request.user.has_perm(
            "quickstart.view_reviews"
        ):  # Use a relevant permission
            self.permission_denied(
                request, message="You cannot view review statistics."
            )

        try:
            base_qs = Reviews.objects.all()

            # --- Status Counts ---
            status_counts = (
                base_qs.values("status").annotate(count=Count("reviewId")).order_by()
            )
            status_dict = {item["status"]: item["count"] for item in status_counts}

            # --- Other Counts ---
            total_reviews = base_qs.count()
            reported_reviews = base_qs.filter(reported=True).count()

            # --- Average Rating ---
            avg_rating_data = base_qs.filter(status="approved").aggregate(
                avg=Avg("rating")
            )
            average_rating = avg_rating_data.get("avg") or 0.0

            # --- Average Moderation Time ---
            # For reviews that have been reported and subsequently responded to/status changed.
            moderated_reviews_qs = base_qs.filter(
                reported=True,
                responded_at__isnull=False,
                reported_at__isnull=False,
                responded_at__gt=F("reported_at"),
            ).annotate(
                mod_time=ExpressionWrapper(
                    F("responded_at") - F("reported_at"),
                    output_field=fields.DurationField(),
                )
            )

            avg_mod_duration = moderated_reviews_qs.aggregate(avg=Avg("mod_time")).get(
                "avg"
            )

            avg_mod_formatted = "N/A"
            if avg_mod_duration:
                total_seconds = avg_mod_duration.total_seconds()
                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60
                if days > 1:
                    avg_mod_formatted = f"{days:.1f} days"
                elif hours > 1:
                    avg_mod_formatted = f"{hours:.1f} hours"
                else:
                    avg_mod_formatted = f"{minutes:.0f} mins"

            data = {
                "total": total_reviews,
                "approved": status_dict.get("approved", 0),
                "underReview": status_dict.get("under_review", 0),
                "hidden": status_dict.get("hidden", 0),
                "reported": reported_reviews,
                "averageRating": average_rating,
                "avgModerationTimeDisplay": avg_mod_formatted,
            }
            return Response(data)

        except Exception as e:
            logger.error(f"Error generating review analytics: {e}", exc_info=True)
            return Response(
                {"error": "Could not generate analytics"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def partial_update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_reviews"):
            self.permission_denied(request, message="You cannot moderate reviews.")

        instance = self.get_object()
        # Define fields an admin can change in one go
        allowed_fields = ["status"]  # Admin can only change status
        update_data = {}

        for field in allowed_fields:
            if field in request.data:
                update_data[field] = request.data[field]

        if not update_data:
            return Response(
                {
                    "detail": "No valid fields provided for update. Only 'status' is allowed."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(instance, data=update_data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        logger.info(
            f"Review ID {instance.pk} updated by Admin {request.user.email}. Changes: {update_data}"
        )

        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def update_status(self, request, pk=None):
        if not request.user.has_perm("quickstart.change_reviews"):
            self.permission_denied(
                request, message="You cannot moderate review status."
            )

        instance = self.get_object()
        new_status = request.data.get("status")

        if not new_status:
            return Response(
                {"error": "Status field is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_statuses = [
            choice[0] for choice in Reviews._meta.get_field("status").choices
        ]
        if new_status not in valid_statuses:
            return Response(
                {
                    "error": f"Invalid status '{new_status}'. Valid options are: {', '.join(valid_statuses)}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = instance.status
        instance.status = new_status
        instance.save(update_fields=["status"])

        logger.info(
            f"Review ID {instance.pk} status changed from {old_status} to {new_status} by Admin {request.user.email} via custom action."
        )

        serializer = self.get_serializer(instance)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_reviews"):
            self.permission_denied(request, message="You cannot delete reviews.")

        instance = self.get_object()
        review_id = instance.pk
        logger.warning(f"Review ID {review_id} deleted by Admin {request.user.email}")

        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
