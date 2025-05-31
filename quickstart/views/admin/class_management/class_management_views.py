# quickstart/views/admin/class_management/class_management_views.py

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
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
)  # Ensure all needed imports are here
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.http import HttpResponse  # For CSV export
import csv  # For CSV export
import logging

from ....models import (
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    BusinessInfo,  # Ensure BusinessInfo is imported if needed for hierarchy checks
)
from ....serializers.admin.class_management.class_management_serializers import (
    AdminClassSerializer,
    AdminClassDetailSerializer,
    # AdminClassCreateSerializer, # Keep if used, commented out if not needed in this file context
    AdminClassCategorySerializer,
    AdminReviewSerializer,
    SubcategorySerializer,
)

# Optional: Import hierarchy helper if needed for actions
# from ..user_management.user_admin_views import user_can_manage


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


# --- AdminClassViewSet ---


class AdminClassViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing classes
    """

    permission_classes = [IsAuthenticated, CanAccessClassAdmin]
    http_method_names = [
        "get",
        "post",
        "patch",
        "delete",
        "head",
        "options",
        "trace",
    ]  # Ensure PATCH is allowed for status update
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "businessId__businessName", "category__name", "location"]
    # Define orderable fields - ensure annotations exist in get_queryset if sorting by them
    ordering_fields = [
        "title",
        "createdAt",
        "average_rating",
        "review_count",
        "active_schedules_count",
        "business_name",
        "status",
        "min_price",  # Added min_price for sorting
    ]
    ordering = ["-createdAt"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminClassDetailSerializer
        # Use AdminClassSerializer for list which includes the annotated fields
        return AdminClassSerializer

    def get_queryset(self):
        """
        Get queryset for admin class views, annotated with necessary metrics.
        Includes prefetch for options__schedules for accurate price fallback.
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
                    # --- FIX: Removed is_active=True from Schedule filter ---
                    Prefetch(
                        "options__schedules",
                        queryset=Schedule.objects.filter(price__isnull=False),
                    ),  # Filter for schedules that have a price for min/max calculations
                    # --- End Fix ---
                    "options__schedules__instances",
                    "reviews",
                    "images",
                )
                .distinct()
            )

            # --- Annotations (Ensure these subqueries are correct for your DB) ---
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
            # Ensure schedules related name is correct and price field exists
            # --- FIX: Removed is_active=True from Schedule filter in subqueries ---
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
            # --- End Fix ---
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

            # Apply Annotations with Coalesce
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
        if not request.user.has_perm("quickstart.view_class_analytics"):
            self.permission_denied(request, message="You cannot view class analytics.")

        try:
            base_qs = ClassesMain.objects.all()
            total_classes = base_qs.count()
            active_classes_count = base_qs.filter(
                status="active"
            ).count()  # Renamed for clarity

            avg_rating_result = Reviews.objects.filter(status="approved").aggregate(
                avg=Coalesce(Avg("rating"), Value(0.0))
            )
            avg_rating = avg_rating_result["avg"]

            category_counts_qs = (
                ClassCategory.objects.annotate(count=Count("classes", distinct=True))
                .values("id", "name", "key", "color", "count")
                .order_by("-count")
            )
            category_counts = list(category_counts_qs)

            status_counts_qs = (
                base_qs.values("status")
                .annotate(count=Count("classId"))
                .order_by("status")
            )
            status_counts = {item["status"]: item["count"] for item in status_counts_qs}

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

            featured_classes_count = base_qs.filter(businessId__featured=True).count()

            return Response(
                {
                    "totalClasses": total_classes,
                    "activeClasses": active_classes_count,  # Use consistent naming
                    "averageRating": round(avg_rating, 1),
                    "categoryCounts": category_counts,
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
    queryset = ClassCategory.objects.prefetch_related("subcategories").order_by("name")
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "key"]

    # --- Standard CRUD with Permissions ---
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
        if instance.classes.exists():
            return Response(
                {
                    "detail": f"Cannot delete category '{instance.name}' as it is used by {instance.classes.count()} classes."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if instance.subcategories.exists():
            return Response(
                {
                    "detail": f"Cannot delete category '{instance.name}' as it has subcategories. Delete subcategories first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        category_name = instance.name
        logger.warning(
            f"Category '{category_name}' (ID: {instance.pk}) deleted by Admin {request.user.email}"
        )

        return super().destroy(request, *args, **kwargs)

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

    @action(detail=False, methods=["get"])
    def stats(self, request):
        if not request.user.has_perm("quickstart.view_classcategory"):
            self.permission_denied(request, message="You cannot view categories.")

        try:
            queryset = self.filter_queryset(self.get_queryset())

            annotated_queryset = queryset.annotate(
                active_classes=Count(
                    "classes", filter=Q(classes__status="active"), distinct=True
                )
            )
            serializer = self.get_serializer(
                annotated_queryset, many=True, context={"request": request}
            )
            return Response(serializer.data)

        except Exception as e:
            logger.error(
                f"Error generating category list with stats: {e}", exc_info=True
            )
            return Response(
                {"error": "Could not retrieve category data"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
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


# --- AdminReviewViewSet ---
class AdminReviewViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, CanAccessReviewAdmin]
    serializer_class = AdminReviewSerializer
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

    def partial_update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_reviews"):
            self.permission_denied(request, message="You cannot moderate reviews.")

        instance = self.get_object()
        allowed_fields = ["status", "business_response", "reported", "report_reason"]
        update_data = {}
        valid_update = False
        for field in allowed_fields:
            if field in request.data:
                update_data[field] = request.data[field]
                valid_update = True

        if not valid_update:
            return Response(
                {"detail": "No valid fields provided for update."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "status" in update_data:
            valid_statuses = [
                choice[0] for choice in Reviews._meta.get_field("status").choices
            ]
            if update_data["status"] not in valid_statuses:
                return Response(
                    {
                        "status": [
                            f"Invalid status. Choose from: {', '.join(valid_statuses)}"
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if "reported" in update_data:
            update_data["reported"] = str(update_data["reported"]).lower() in [
                "true",
                "1",
                "yes",
            ]
            if not update_data["reported"] and "report_reason" not in update_data:
                update_data["report_reason"] = ""

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
