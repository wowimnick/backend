from datetime import timedelta
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.authentication import TokenAuthentication
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import JSONParser, FormParser
from rest_framework.exceptions import ValidationError
import json
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

from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessClassAdmin,
    CanAccessCategoryAdmin,
    CanAccessReviewAdmin,
)
from quickstart.utils.revalidation import trigger_nextjs_revalidation
from quickstart.models import (
    ClassCategory,
    ClassCollection,
    ClassImage,
    ClassSubcategory,
    ClassesMain,
    ClassOption,
    ImportedGoogleReview,
    Payment,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    BusinessInfo,
    VerificationRequest,  # Ensure BusinessInfo is imported if needed for hierarchy checks
)
from quickstart.serializers.admin.class_management.class_management_serializers import (
    AdminClassCollectionSerializer,
    AdminClassSerializer,
    AdminClassDetailSerializer,
    AdminClassCategorySerializer,
    AdminReviewSerializer,
    ReassignmentSerializer,
    SubcategorySerializer,
)

from quickstart.serializers import ManagedClassOptionSerializer, ManagedClassSerializer

logger = logging.getLogger(__name__)


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
        if self.action in ["update", "partial_update"]:
            return ManagedClassSerializer
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
        UPDATED: Now includes Google reviews in review count
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
                    # ADD THIS LINE HERE:
                    "collections",
                    Prefetch(
                        "images",
                        queryset=ClassImage.objects.order_by("-isCover", "createdAt"),
                    ),
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

            # Platform review count
            approved_review_count_subquery = Subquery(
                Reviews.objects.filter(classId=OuterRef("pk"), status="approved")
                .values("classId")
                .annotate(c=Count("pk"))
                .values("c"),
                output_field=Count("pk").output_field,
            )

            # NEW: Google review count
            google_review_count_subquery = Subquery(
                ImportedGoogleReview.objects.filter(business=OuterRef("businessId"))
                .values("business")
                .annotate(count=Count("id"))
                .values("count")[:1],
                output_field=fields.IntegerField(),
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
                .values("booking__schedule_instance__schedule__option__classId")
                .annotate(total_fees=Sum("platform_fee_amount"))
                .values("total_fees")[:1],
                output_field=DecimalField(),
            )

            queryset = queryset.annotate(
                business_name=F("businessId__businessName"),
                business_featured=F("businessId__featured"),
                average_rating=Coalesce(
                    approved_rating_subquery, Value(0.0), output_field=FloatField()
                ),
                platform_review_count=Coalesce(
                    approved_review_count_subquery,
                    Value(0),
                    output_field=Count("pk").output_field,
                ),
                google_review_count=Coalesce(
                    google_review_count_subquery,
                    Value(0),
                    output_field=fields.IntegerField(),
                ),
                review_count=ExpressionWrapper(
                    F("platform_review_count") + F("google_review_count"),
                    output_field=fields.IntegerField(),
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

    def update(self, request, *args, **kwargs):
        """
        Updates a class, including images, options, and collections.
        """
        if not request.user.has_perm("quickstart.change_classesmain"):
            self.permission_denied(request, message="You cannot update class details.")

        instance = self.get_object()
        request_data = request.data

        # Use a transaction to ensure all or no changes are saved
        with transaction.atomic():
            # 1. Update the main ClassesMain instance fields via Serializer
            serializer = self.get_serializer(instance, data=request_data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated_instance = serializer.save()
            logger.info(
                f"Admin {request.user.email} started updating Class '{instance.title}' (ID: {instance.pk})."
            )

            # 2. Handle Collections Update (Explicitly)
            if "collections" in request_data:
                collection_ids = request_data.get("collections")
                if isinstance(collection_ids, list):
                    # .set() handles the M2M relationship using IDs
                    instance.collections.set(collection_ids)
                    logger.info(f"Updated collections for class {instance.pk} to {collection_ids}")

            # 3. Handle Image Deletions
            try:
                delete_image_ids = json.loads(
                    request_data.get("delete_image_ids", "[]")
                )
                if delete_image_ids:
                    ClassImage.objects.filter(
                        classId=instance, imageId__in=delete_image_ids
                    ).delete()
            except (json.JSONDecodeError, TypeError):
                pass

            # 4. Handle New Image Additions from S3 keys
            try:
                new_image_s3_keys = json.loads(
                    request_data.get("new_image_s3_keys", "[]")
                )
                if new_image_s3_keys:
                    images_to_create = [
                        ClassImage(classId=instance, image=key, isCover=False)
                        for key in new_image_s3_keys
                    ]
                    ClassImage.objects.bulk_create(images_to_create)
            except (json.JSONDecodeError, TypeError):
                pass

            # 5. Handle Cover Image Assignment
            cover_image_id = request_data.get("cover_image_id")
            if cover_image_id:
                ClassImage.objects.filter(classId=instance, isCover=True).update(
                    isCover=False
                )
                ClassImage.objects.filter(
                    classId=instance, imageId=cover_image_id
                ).update(isCover=True)

            # 6. Handle ClassOption Update
            options_json_string = request_data.get("options")
            if options_json_string:
                try:
                    options_data = json.loads(options_json_string)[0]
                    option_instance = instance.options.first()
                    if option_instance:
                        option_serializer = ManagedClassOptionSerializer(
                            instance=option_instance, data=options_data, partial=True
                        )
                        option_serializer.is_valid(raise_exception=True)
                        option_serializer.save()
                except (json.JSONDecodeError, IndexError, TypeError) as e:
                    logger.error(f"Error processing options: {e}")
                    raise ValidationError({"options": "Invalid options data."})

        # --- Trigger Revalidation ---
        if hasattr(self, '_trigger_class_revalidation'):
            self._trigger_class_revalidation(updated_instance)

        # Return the fully updated object (detail serializer includes collections)
        detail_serializer = AdminClassDetailSerializer(
            instance, context={"request": request}
        )
        return Response(detail_serializer.data)

    def destroy(self, request, *args, **kwargs):
        """
        UPDATED: Add revalidation after class deletion
        """
        if not request.user.has_perm("quickstart.delete_classesmain"):
            self.permission_denied(request, message="You cannot delete classes.")

        instance = self.get_object()
        class_title = instance.title

        # Store info before deletion for revalidation
        class_slug = instance.slug if hasattr(instance, "slug") else None
        business_slug = (
            instance.businessId.slug
            if instance.businessId and hasattr(instance.businessId, "slug")
            else None
        )
        category_key = (
            instance.category.key
            if instance.category and hasattr(instance.category, "key")
            else None
        )
        subcategory_key = (
            instance.subcategory.key
            if instance.subcategory and hasattr(instance.subcategory, "key")
            else None
        )

        logger.warning(
            f"Class '{class_title}' (ID: {instance.pk}) deleted by Admin {request.user.email}"
        )

        instance.delete()

        # --- ADDED: Trigger revalidation after deletion ---
        if class_slug:
            trigger_nextjs_revalidation(tag=f"class-{class_slug}")
        if business_slug:
            trigger_nextjs_revalidation(tag=f"business-{business_slug}")

        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag="classes")

        if category_key:
            trigger_nextjs_revalidation(tag=f"category-{category_key}")
        if subcategory_key:
            trigger_nextjs_revalidation(tag=f"subcategory-{subcategory_key}")

        logger.info(f"Revalidated pages after deletion of class '{class_title}'")

        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- Custom Actions ---

    @action(detail=True, methods=["patch", "post"])
    def update_class_status(self, request, pk=None):
        """
        Manually update the status of a class (e.g., active, suspended).
        """
        # Ensure the user has permission (adjust permission codename if needed)
        if not request.user.has_perm("quickstart.change_classesmain"):
             self.permission_denied(request, message="You cannot change class status.")
        
        instance = self.get_object()
        new_status = request.data.get("status")
        reason = request.data.get("reason", "")

        # Basic validation
        valid_statuses = dict(ClassesMain.STATUS_CHOICES).keys()
        if new_status not in valid_statuses:
             return Response(
                 {"error": f"Invalid status. Choices are: {', '.join(valid_statuses)}"}, 
                 status=status.HTTP_400_BAD_REQUEST
             )

        old_status = instance.status
        instance.status = new_status
        instance.save(update_fields=["status"])
        
        # Trigger revalidation since visibility changed
        if hasattr(self, '_trigger_class_revalidation'):
            self._trigger_class_revalidation(instance)

        logger.info(f"Class {instance.classId} status changed from {old_status} to {new_status} by {request.user.email}. Reason: {reason}")
        
        return Response({"status": "success", "new_status": new_status, "classId": instance.classId})
    
    @action(detail=False, methods=["get"])
    def analytics(self, request):
        """
        Provides high-level statistics for the Class Management dashboard.
        UPDATED: Now includes schedule warnings and Google reviews
        """
        if not request.user.has_perm("quickstart.view_class_analytics"):
            self.permission_denied(request, message="You cannot view class analytics.")

        try:
            # --- Class Stats ---
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

            # --- NEW: Schedule Warning Stats (show when < 2 weeks of schedules left) ---
            two_weeks_from_now = timezone.now() + timedelta(days=14)

            classes_with_low_schedules = []
            active_classes = base_qs.filter(
                status="active", businessId__isActive=True
            ).select_related("businessId", "businessId__owner")

            for cls in active_classes:
                # Get latest schedule instance
                latest_instance = (
                    ScheduleInstance.objects.filter(
                        schedule__option__classId=cls, date__gte=timezone.now().date()
                    )
                    .order_by("-date")
                    .first()
                )

                if (
                    latest_instance is None
                    or latest_instance.date < two_weeks_from_now.date()
                ):
                    classes_with_low_schedules.append(
                        {
                            "classId": cls.classId,
                            "title": cls.title,
                            "businessName": (
                                cls.businessId.businessName if cls.businessId else "N/A"
                            ),
                            "businessEmail": (
                                cls.businessId.owner.email
                                if cls.businessId and cls.businessId.owner
                                else "N/A"
                            ),
                            "ownerId": (
                                cls.businessId.owner.userId
                                if cls.businessId and cls.businessId.owner
                                else None
                            ),
                            "lastScheduleDate": (
                                latest_instance.date.isoformat()
                                if latest_instance
                                else None
                            ),
                            "daysRemaining": (
                                (latest_instance.date - timezone.now().date()).days
                                if latest_instance
                                else 0
                            ),
                        }
                    )

            schedule_warnings_count = len(classes_with_low_schedules)

            # --- UPDATED: Review Stats (Platform + Google) ---
            platform_reviews = Reviews.objects.filter(status="approved")
            platform_avg_rating = platform_reviews.aggregate(
                avg=Coalesce(Avg("rating"), Value(0.0))
            )["avg"]
            platform_review_count = platform_reviews.count()

            google_reviews = ImportedGoogleReview.objects.all()
            google_avg_rating = google_reviews.aggregate(
                avg=Coalesce(Avg("rating"), Value(0.0))
            )["avg"]
            google_review_count = google_reviews.count()

            # Combined weighted average
            total_review_count = platform_review_count + google_review_count
            if total_review_count > 0:
                combined_avg_rating = (
                    (platform_avg_rating * platform_review_count)
                    + (google_avg_rating * google_review_count)
                ) / total_review_count
            else:
                combined_avg_rating = 0.0

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
                    "activeClasses": active_classes_count,
                    "averageRating": round(combined_avg_rating, 1),
                    "totalReviews": total_review_count,
                    "platformReviews": platform_review_count,
                    "googleReviews": google_review_count,
                    "scheduleWarningsCount": schedule_warnings_count,
                    "classesWithLowSchedules": classes_with_low_schedules[:10],
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
    
    def _trigger_class_revalidation(self, class_instance):
        """Helper to trigger all necessary revalidations for a class."""
        if not class_instance:
            return

        # 1. Revalidate the class detail page by tags
        if hasattr(class_instance, "slug") and class_instance.slug:
            trigger_nextjs_revalidation(tag=f"class-{class_instance.slug}")

        trigger_nextjs_revalidation(tag=f"class-{class_instance.classId}")

        # 2. Revalidate the business page if the class belongs to a business
        if class_instance.businessId and hasattr(class_instance.businessId, "slug"):
            business_slug = class_instance.businessId.slug
            trigger_nextjs_revalidation(tag=f"business-{business_slug}")
            logger.info(f"Revalidated business page: business-{business_slug}")

        # 3. Revalidate homepage and search/explore pages
        trigger_nextjs_revalidation(path="/")

        tags_to_revalidate = ["classes-search", "homepage-classes", "classes"]
        if class_instance.category and hasattr(class_instance.category, "key"):
            tags_to_revalidate.append(f"category-{class_instance.category.key}")
        if class_instance.subcategory and hasattr(class_instance.subcategory, "key"):
            tags_to_revalidate.append(f"subcategory-{class_instance.subcategory.key}")

        for tag in tags_to_revalidate:
            trigger_nextjs_revalidation(tag=tag)

        logger.info(
            f"Triggered revalidation for class {class_instance.pk} (slug: {class_instance.slug}) and related tags."
        )


# --- AdminCategoryViewSet ---
class AdminCategoryViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing class categories
    """

    permission_classes = [IsAuthenticated, CanAccessCategoryAdmin]
    serializer_class = AdminClassCategorySerializer
    queryset = ClassCategory.objects.all()
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "key"]
    # MODIFIED: Removed MultiPartParser to disallow direct file uploads.
    parser_classes = [JSONParser, FormParser]

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

        # --- ADDED: Trigger revalidation after category creation ---
        if response.status_code == status.HTTP_201_CREATED:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="classes-search")
            trigger_nextjs_revalidation(tag="homepage-categories")
            trigger_nextjs_revalidation(tag="categories")
            trigger_nextjs_revalidation(tag="business-categories")
            trigger_nextjs_revalidation(tag="homepage-classes")
            logger.info(f"Revalidated homepage and class pages after category creation")

        return response

    def update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_classcategory"):
            self.permission_denied(request, message="You cannot update categories.")

        instance = self.get_object()
        old_name = instance.name
        old_key = instance.key  # Capture old key for potential revalidation

        response = super().update(request, *args, **kwargs)

        if response.status_code == status.HTTP_200_OK:
            logger.info(
                f"Category '{old_name}' (ID: {instance.pk}) updated by Admin {request.user.email}"
            )

            # --- ADDED: Trigger revalidation after category update ---
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="classes-search")
            trigger_nextjs_revalidation(tag="homepage-categories")
            trigger_nextjs_revalidation(tag="categories")
            trigger_nextjs_revalidation(tag="business-categories")
            trigger_nextjs_revalidation(tag="homepage-classes")

            # Revalidate old category key if it changed
            instance.refresh_from_db()
            if old_key != instance.key:
                trigger_nextjs_revalidation(tag=f"category-{old_key}")

            # Revalidate new/current category key
            trigger_nextjs_revalidation(tag=f"category-{instance.key}")

            logger.info(f"Revalidated homepage and class pages after category update")

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
        category_key = instance.key  # Capture before deletion

        instance.delete()

        logger.warning(
            f"Category '{category_name}' (ID: {instance.pk}) with no classes deleted by Admin {request.user.email}"
        )

        # --- ADDED: Trigger revalidation after category deletion ---
        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-categories")
        trigger_nextjs_revalidation(tag="categories")
        trigger_nextjs_revalidation(tag="business-categories")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{category_key}")

        logger.info(f"Revalidated homepage and class pages after category deletion")

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
        category_key = category_to_delete.key  # Capture before deletion
        new_category_key = new_category.key

        category_to_delete.delete()

        logger.warning(
            f"Category '{category_name}' (ID: {pk}) deleted after reassigning classes by Admin {request.user.email}."
        )

        # --- ADDED: Trigger revalidation after category deletion with reassignment ---
        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{category_key}")
        trigger_nextjs_revalidation(tag=f"category-{new_category_key}")

        logger.info(
            f"Revalidated homepage and class pages after category reassignment and deletion"
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
            # MODIFIED: Fetch the new subcategory globally, not restricted to the current category.
            new_subcategory = ClassSubcategory.objects.get(
                pk=new_subcategory_id
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

        # MODIFIED: Update BOTH subcategory and category to match the new subcategory's parent.
        updated_count = ClassesMain.objects.filter(
            subcategory=subcategory_to_delete
        ).update(
            subcategory=new_subcategory,
            category=new_subcategory.category # Ensure the parent category changes too
        )

        logger.info(
            f"{updated_count} classes reassigned from Subcategory '{subcategory_to_delete.name}' "
            f"(Cat: {category.name}) to '{new_subcategory.name}' (Cat: {new_subcategory.category.name})."
        )

        subcategory_name = subcategory_to_delete.name
        old_category_key = category.key
        new_category_key = new_subcategory.category.key

        subcategory_to_delete.delete()

        logger.warning(
            f"Subcategory '{subcategory_name}' deleted after reassigning classes by Admin {request.user.email}."
        )

        # --- ADDED: Trigger revalidation for homepage, search, old category, and new category ---
        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{old_category_key}")
        if old_category_key != new_category_key:
            trigger_nextjs_revalidation(tag=f"category-{new_category_key}")

        return Response(
            {
                "detail": f"Successfully reassigned {updated_count} classes and deleted subcategory '{subcategory_name}'."
            },
            status=status.HTTP_200_OK,
        )
    
    @action(detail=False, methods=["post"], url_path="update-order")
    def update_order(self, request):
        """
        Receives a list of category IDs in their desired order and updates their sort_order.
        Expects a payload like: [{"id": 1, "order": 0}, {"id": 3, "order": 1}]
        """
        if not request.user.has_perm("quickstart.change_classcategory"):
            self.permission_denied(request, message="You cannot reorder categories.")

        ordered_data = request.data
        if not isinstance(ordered_data, list):
            return Response(
                {"error": "Expected a list of category objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                for item in ordered_data:
                    category_id = item.get("id")
                    new_order = item.get("order")
                    if category_id is not None and new_order is not None:
                        ClassCategory.objects.filter(pk=category_id).update(
                            sort_order=new_order
                        )

            logger.info(f"Category order updated by Admin {request.user.email}.")
            return Response({"status": "success"}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Failed to update category order: {e}", exc_info=True)
            return Response(
                {"error": "An internal error occurred while updating the order."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        
class AdminCollectionViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing Class Collections (Vibes).
    """
    permission_classes = [IsAuthenticated, CanAccessCategoryAdmin] # Re-use category permissions
    serializer_class = AdminClassCollectionSerializer
    # Order by sort_order so drag-and-drop reflects correctly in initial fetch
    queryset = ClassCollection.objects.all().order_by('sort_order')
    parser_classes = [JSONParser, FormParser]

    def get_queryset(self):
        # Annotate with the number of classes in this collection
        return ClassCollection.objects.annotate(
            class_count=Count('classes', distinct=True) 
        ).order_by('sort_order')

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        if response.status_code == status.HTTP_201_CREATED:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            logger.info(f"Created collection and triggered revalidation")
        return response

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            logger.info(f"Updated collection and triggered revalidation")
        return response

    def destroy(self, request, *args, **kwargs):
        response = super().destroy(request, *args, **kwargs)
        if response.status_code == status.HTTP_204_NO_CONTENT:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            logger.info(f"Deleted collection and triggered revalidation")
        return response

    @action(detail=False, methods=["post"], url_path="update-order")
    def update_order(self, request):
        """
        Update sort_order for collections.
        """
        ordered_data = request.data
        if not isinstance(ordered_data, list):
            return Response({"error": "Expected a list."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                for item in ordered_data:
                    c_id = item.get("id")
                    order = item.get("order")
                    if c_id is not None and order is not None:
                        ClassCollection.objects.filter(pk=c_id).update(sort_order=order)
            
            trigger_nextjs_revalidation(tag="homepage-content")
            return Response({"status": "success"})
        except Exception as e:
            logger.error(f"Failed to update collection order: {e}")
            return Response({"error": "Internal error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)




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
