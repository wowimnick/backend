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
from django.shortcuts import get_object_or_404
from django.core.files.storage import default_storage
from django.db.models import (
    Q,
    Min,
    Max,
    Avg,
    Case,
    Count,
    DateField,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Prefetch,
    Subquery,
    Sum,
    Value,
    When,
    fields,
)
from decimal import Decimal
from django.db.models.functions import Cast, Coalesce
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
from django.core.cache import cache

from quickstart.utils.revalidation import trigger_nextjs_revalidation
from quickstart.views.public.public_class_views import (
    invalidate_public_class_search_preset_cache,
    HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY,
)
from quickstart.views.public.public_business_views import invalidate_business_detail_cache
from quickstart.views.admin.metrics_time_windows import get_admin_metrics_local_now
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

from quickstart.serializers.public.public_review_serializers import (
    ImportedGoogleReviewSerializer,
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
    search_fields = ["title", "businessId__businessName", "location"]
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
            action = getattr(self, "action", None) or ""
            queryset = ClassesMain.objects.select_related(
                "businessId", "businessId__owner"
            )

            # Avoid DISTINCT on list/search paths: Postgres pays a steep sort/hash penalty for
            # DISTINCT × SearchFilter OR; only collapse duplicates from M2M collection filter.

            # Avoid loading options/schedules/instances for list — not present on list serializer
            # and was the main cause of slow admin class retrieve (huge JSON + DB work).
            if action == "list":
                queryset = queryset.prefetch_related(
                    "collections",
                    Prefetch(
                        "images",
                        queryset=ClassImage.objects.order_by("-isCover", "createdAt"),
                    ),
                )
            else:
                queryset = queryset.prefetch_related(
                    Prefetch(
                        "options__schedules",
                        queryset=Schedule.objects.filter(price__isnull=False),
                    ),
                    "collections",
                    Prefetch(
                        "images",
                        queryset=ClassImage.objects.order_by("-isCover", "createdAt"),
                    ),
                )

            # --- Annotations ---
            raw_ord = (
                self.request.query_params.get(filters.OrderingFilter.ordering_param) or ""
            ).strip()
            if raw_ord:
                ordering_tokens_lower = {
                    tok.lstrip("-").strip().lower()
                    for tok in raw_ord.split(",")
                    if tok.strip()
                }
            else:
                ordering_tokens_lower = {
                    str(o).lstrip("-").lower()
                    for o in (
                        self.ordering
                        if isinstance(self.ordering, (list, tuple))
                        else [self.ordering]
                    )
                }

            # Paying correlated Payment subqueries only when sorting revenue (otherwise list UX
            # shows 0.00 lifetime platform fees — acceptable vs 7s waits on every keystroke/search).
            annotate_platform_revenue = (
                action != "list" or "platform_revenue" in ordering_tokens_lower
            )

            # Platform/Google rollups rely on stored denorms (same as marketplace) so list rows
            # avoid ImportedGoogleReviews + Reviews AVG correlated subqueries per class.
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
                .values(ai_group=Value(1))
                .annotate(c=Count("id"))
                .values("c")[:1],
                output_field=IntegerField(),
            )

            # Latest booked day among future instances (listing horizon for admin UX).
            furthest_future_instance_subquery = Subquery(
                ScheduleInstance.objects.filter(
                    schedule__option__classId=OuterRef("pk"),
                    date__gte=timezone.now().date(),
                    status="scheduled",
                )
                .values(ff_group=Value(1))
                .annotate(maxd=Max("date"))
                .values("maxd")[:1],
                output_field=DateField(),
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
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )

            queryset = queryset.annotate(
                business_name=F("businessId__businessName"),
                business_featured=F("businessId__featured"),
                google_avg_rating=Coalesce(
                    Cast(F("businessId__google_avg_rating"), FloatField()),
                    Value(0.0),
                    output_field=FloatField(),
                ),
                google_review_count=Coalesce(
                    F("businessId__google_review_count"),
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
                    output_field=IntegerField(),
                ),
                furthest_future_instance_date=furthest_future_instance_subquery,
                platform_revenue=(
                    Coalesce(
                        platform_revenue_subquery,
                        Value(Decimal("0.00")),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                    if annotate_platform_revenue
                    else Value(
                        Decimal("0.00"),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    )
                ),
            )
            # Combined average from denormalized platform class stats + imported Google rollup on business.
            queryset = queryset.annotate(
                average_rating=Case(
                    When(
                        review_count__gt=0,
                        then=ExpressionWrapper(
                            (
                                Coalesce(
                                    Cast(F("platform_avg_rating"), FloatField()),
                                    Value(0.0),
                                    output_field=FloatField(),
                                )
                                * Cast(F("platform_review_count"), FloatField())
                                + F("google_avg_rating")
                                * Cast(F("google_review_count"), FloatField())
                            )
                            / Cast(F("review_count"), FloatField()),
                            output_field=FloatField(),
                        ),
                    ),
                    default=Value(0.0),
                    output_field=FloatField(),
                ),
            )

            # --- Filtering Logic ---

            status_filter = self.request.query_params.get("status")
            if status_filter and status_filter != "all":
                valid_statuses = [choice[0] for choice in ClassesMain.STATUS_CHOICES]
                if status_filter in valid_statuses:
                    queryset = queryset.filter(status=status_filter)

            business_id = self.request.query_params.get("business_id")
            if business_id:
                try:
                    queryset = queryset.filter(businessId_id=int(business_id))
                except (TypeError, ValueError):
                    pass

            # Multi-collection filter: class is included if it belongs to ANY selected collection.
            collection_ids = []
            for val in self.request.query_params.getlist("collection_ids"):
                for piece in str(val).split(","):
                    piece = piece.strip()
                    if piece.isdigit():
                        collection_ids.append(int(piece))
            collection_ids = list(dict.fromkeys(collection_ids))
            if collection_ids:
                queryset = queryset.filter(collections__id__in=collection_ids).distinct()

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
            instance = updated_instance
            logger.info(
                f"Admin {request.user.email} started updating Class '{instance.title}' (ID: {instance.pk})."
            )

            # 2. Handle Collections Update (Explicitly)
            if "collections" in request_data:
                collection_ids = request_data.get("collections")
                if isinstance(collection_ids, list):
                    instance.collections.set(collection_ids)
                    logger.info(
                        f"Updated collections for class {instance.pk} to {collection_ids}"
                    )

            # 3. Image deletions (S3 + DB) — align with business dashboard
            delete_image_ids_str = request_data.get("delete_image_ids", "[]")
            try:
                delete_image_ids = json.loads(delete_image_ids_str)
                if delete_image_ids:
                    images_to_delete = ClassImage.objects.filter(
                        classId=instance, imageId__in=delete_image_ids
                    )
                    for img in images_to_delete:
                        if img.image and img.image.name:
                            default_storage.delete(img.image.name)
                    deleted_count, _ = images_to_delete.delete()
                    if deleted_count:
                        logger.info(
                            f"Admin deleted {deleted_count} ClassImage records for class {instance.pk}."
                        )
            except json.JSONDecodeError:
                logger.warning(
                    f"Admin: could not parse delete_image_ids: {delete_image_ids_str}"
                )

            # 4. New images from S3 keys
            new_image_s3_keys_str = request_data.get("new_image_s3_keys", "[]")
            try:
                new_image_s3_keys = json.loads(new_image_s3_keys_str)
                if new_image_s3_keys:
                    img_objects = [
                        ClassImage(classId=instance, image=key, isCover=False)
                        for key in new_image_s3_keys
                    ]
                    ClassImage.objects.bulk_create(img_objects)
                    logger.info(
                        f"Admin bulk-added {len(img_objects)} new images for class {instance.pk}."
                    )
            except json.JSONDecodeError:
                logger.warning(
                    f"Admin: could not parse new_image_s3_keys: {new_image_s3_keys_str}"
                )

            # 5. Cover image
            cover_image_id_str = request_data.get("cover_image_id")
            cover_image_s3_key = request_data.get("cover_image_s3_key")
            ClassImage.objects.filter(classId=instance, isCover=True).update(
                isCover=False
            )
            if cover_image_s3_key:
                ClassImage.objects.filter(
                    classId=instance, image=cover_image_s3_key
                ).update(isCover=True)
            elif cover_image_id_str:
                ClassImage.objects.filter(
                    classId=instance, imageId=int(cover_image_id_str)
                ).update(isCover=True)
            if not ClassImage.objects.filter(classId=instance, isCover=True).exists():
                first_image = (
                    ClassImage.objects.filter(classId=instance)
                    .order_by("createdAt")
                    .first()
                )
                if first_image:
                    first_image.isCover = True
                    first_image.save(update_fields=["isCover"])

            _min_images = 4
            if ClassImage.objects.filter(classId=instance).count() < _min_images:
                raise ValidationError(
                    {"images": f"At least {_min_images} class images are required."}
                )

            # 6. Multi-tier options (smart sync) — align with business dashboard
            options_json_string = request_data.get("options")
            if options_json_string:
                try:
                    options_data_list = json.loads(options_json_string)
                    if not isinstance(options_data_list, list):
                        raise ValidationError(
                            {"options": "Options data must be a list."}
                        )
                    incoming_ids = [
                        item.get("optionId")
                        for item in options_data_list
                        if item.get("optionId")
                    ]
                    if len(options_data_list) > 0:
                        ClassOption.objects.filter(classId=instance).exclude(
                            optionId__in=incoming_ids
                        ).delete()
                    for index, option_dict in enumerate(options_data_list):
                        option_id = option_dict.get("optionId")
                        if index == 0:
                            option_dict["schedule_mode"] = "primary"
                        if option_id:
                            option_instance = get_object_or_404(
                                ClassOption, optionId=option_id, classId=instance
                            )
                            option_serializer = ManagedClassOptionSerializer(
                                option_instance, data=option_dict, partial=True
                            )
                        else:
                            option_serializer = ManagedClassOptionSerializer(
                                data=option_dict
                            )
                        option_serializer.is_valid(raise_exception=True)
                        option_serializer.save(classId=instance)
                except ValidationError:
                    raise
                except Exception as e:
                    logger.error(
                        f"Admin: error processing options for class {instance.pk}: {e}",
                        exc_info=True,
                    )
                    raise ValidationError(
                        {"options": f"Failed to update class options: {str(e)}"}
                    )

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
            local_now = get_admin_metrics_local_now()
            two_weeks_from_now = local_now + timedelta(days=14)
            today = local_now.date()

            # Schedule warnings: low/no future scheduled instances (< 2 weeks runway).
            # Includes inactive & suspended classes and inactive businesses (full operational picture).
            latest_instance_date_subquery = Subquery(
                ScheduleInstance.objects.filter(
                    schedule__option__classId=OuterRef("pk"),
                    date__gte=today,
                    status="scheduled",
                )
                .order_by("-date")
                .values("date")[:1],
                output_field=DateField(),
            )
            schedule_warn_before = two_weeks_from_now.date()
            classes_with_latest_instance = (
                base_qs.select_related("businessId", "businessId__owner")
                .annotate(latest_instance_date=latest_instance_date_subquery)
                .filter(
                    Q(latest_instance_date__isnull=True)
                    | Q(latest_instance_date__lt=schedule_warn_before)
                )
            )
            classes_with_low_schedules = []
            for cls in classes_with_latest_instance:
                latest_date = cls.latest_instance_date
                classes_with_low_schedules.append(
                    {
                        "classId": cls.classId,
                        "title": cls.title,
                        "status": cls.status,
                        "businessIsActive": (
                            bool(cls.businessId.isActive)
                            if cls.businessId
                            else False
                        ),
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
                        # Furthest future scheduled instance (schedule "runway" end), not "next occurrence".
                        "lastScheduleDate": (
                            latest_date.isoformat() if latest_date else None
                        ),
                        "furthestScheduledSessionDate": (
                            latest_date.isoformat() if latest_date else None
                        ),
                        "daysRemaining": (
                            (latest_date - today).days if latest_date else 0
                        ),
                    }
                )

            # Worst first: no future scheduled instances, then soonest last date.
            classes_with_low_schedules.sort(
                key=lambda row: (
                    0 if row["lastScheduleDate"] is None else 1,
                    row["lastScheduleDate"] or "",
                    row["classId"],
                )
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
                    "classesWithLowSchedules": classes_with_low_schedules,
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

        # 2. Revalidate the business page and invalidate Django API cache if the class belongs to a business
        if class_instance.businessId and hasattr(class_instance.businessId, "slug"):
            business_slug = class_instance.businessId.slug
            invalidate_business_detail_cache(business_slug)
            trigger_nextjs_revalidation(tag=f"business-{business_slug}")
            trigger_nextjs_revalidation(path=f"/business/{business_slug}")
            logger.info("Revalidated business page: business-%s and path /business/%s", business_slug, business_slug)

        # 3. Revalidate homepage and search/explore pages
        trigger_nextjs_revalidation(path="/")

        tags_to_revalidate = ["classes-search", "homepage-classes", "classes"]

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
        # ClassesMain no longer has category/subcategory FK; no class counts to annotate.
        return super().get_queryset()

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.add_classcategory"):
            self.permission_denied(request, message="You cannot create categories.")

        response = super().create(request, *args, **kwargs)

        # --- ADDED: Trigger revalidation and backend cache invalidation + prewarm ---
        if response.status_code == status.HTTP_201_CREATED:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="classes-search")
            trigger_nextjs_revalidation(tag="homepage-categories")
            trigger_nextjs_revalidation(tag="categories")
            trigger_nextjs_revalidation(tag="business-categories")
            trigger_nextjs_revalidation(tag="homepage-classes")
            try:
                new_key = response.data.get("key") if response.data else None
                invalidate_public_class_search_preset_cache()
            except Exception as e:
                logger.warning("Failed to invalidate search cache after category create: %s", e)
            logger.info("Revalidated homepage and class pages after category creation + prewarm")

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

            try:
                invalidate_public_class_search_preset_cache()
            except Exception as e:
                logger.warning("Failed to invalidate search cache after category update: %s", e)
            logger.info("Revalidated homepage and class pages after category update + prewarm")

        return response

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_classcategory"):
            self.permission_denied(request, message="You cannot delete categories.")

        instance = self.get_object()

        # ClassesMain no longer has category FK; no classes reference categories.
        category_name = instance.name
        category_key = instance.key  # Capture before deletion

        instance.delete()

        logger.warning(
            f"Category '{category_name}' (ID: {instance.pk}) with no classes deleted by Admin {request.user.email}"
        )

        # --- ADDED: Trigger revalidation and backend cache invalidation + prewarm ---
        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-categories")
        trigger_nextjs_revalidation(tag="categories")
        trigger_nextjs_revalidation(tag="business-categories")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{category_key}")
        try:
            invalidate_public_class_search_preset_cache()
        except Exception as e:
            logger.warning("Failed to invalidate search cache after category delete: %s", e)
        logger.info("Revalidated homepage and class pages after category deletion + prewarm")

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

        # ClassesMain no longer has category/subcategory FK; no reassignment to perform.
        updated_count = 0
        category_name = category_to_delete.name
        category_key = category_to_delete.key
        new_category_key = new_category.key

        category_to_delete.delete()

        logger.warning(
            f"Category '{category_name}' (ID: {pk}) deleted (reassignment no-op; classes no longer use category) by Admin {request.user.email}."
        )

        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{category_key}")
        trigger_nextjs_revalidation(tag=f"category-{new_category_key}")

        logger.info(
            "Revalidated homepage and class pages after category deletion"
        )

        return Response(
            {
                "detail": f"Successfully deleted category '{category_name}'."
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

            # ClassesMain no longer has subcategory FK; no classes reference subcategories.
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

        # ClassesMain no longer has subcategory/category FK; no reassignment to perform.
        updated_count = 0
        subcategory_name = subcategory_to_delete.name
        old_category_key = category.key
        new_category_key = new_subcategory.category.key

        subcategory_to_delete.delete()

        logger.warning(
            f"Subcategory '{subcategory_name}' deleted (reassignment no-op; classes no longer use subcategory) by Admin {request.user.email}."
        )

        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="classes-search")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag=f"category-{old_category_key}")
        if old_category_key != new_category_key:
            trigger_nextjs_revalidation(tag=f"category-{new_category_key}")

        return Response(
            {
                "detail": f"Successfully deleted subcategory '{subcategory_name}'."
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

            try:
                invalidate_public_class_search_preset_cache()
            except Exception as e:
                logger.warning("Failed to invalidate search cache after category order update: %s", e)
            logger.info("Category order updated by Admin %s.", request.user.email)
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
        qs = ClassCollection.objects.annotate(
            class_count=Count("classes", distinct=True)
        ).order_by("sort_order")
        parent_param = self.request.query_params.get("parent")
        if parent_param == "null":
            qs = qs.filter(parent__isnull=True)
        elif parent_param not in (None, ""):
            try:
                pid = int(parent_param)
                qs = qs.filter(parent_id=pid)
            except (TypeError, ValueError):
                pass
        return qs

    def _invalidate_collection_caches(self, collection_slug=None):
        """Invalidate backend caches so collection list and search are updated; trigger prewarm.
        If collection_slug is set, only that collection is prewarmed; otherwise full prewarm."""
        cache.delete(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
        try:
            invalidate_public_class_search_preset_cache(
                affected_collection_slugs=[collection_slug] if collection_slug else None
            )
        except Exception as e:
            logger.warning("Failed to invalidate search cache after collection change: %s", e)

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        if response.status_code == status.HTTP_201_CREATED:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            trigger_nextjs_revalidation(tag="collections")
            new_slug = response.data.get("slug") if response.data else None
            self._invalidate_collection_caches(collection_slug=new_slug)
            logger.info("Created collection and triggered revalidation + prewarm")
        return response

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        response = super().update(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            trigger_nextjs_revalidation(tag="collections")
            self._invalidate_collection_caches(collection_slug=instance.slug)
            logger.info("Updated collection and triggered revalidation + prewarm")
        return response

    @action(detail=True, methods=["post"], url_path="reclassify")
    def reclassify(self, request, pk=None):
        """
        Queue Gemini-based membership pass for this automated collection only (all active classes).
        Does not run on save — explicit admin trigger only.
        """
        collection = self.get_object()
        if collection.type != "automated":
            return Response(
                {"detail": "Only automated collections can run AI curator reclassification."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw_rules = collection.automation_rules if isinstance(collection.automation_rules, dict) else {}
        ai_criteria = (raw_rules.get("ai_criteria") or "").strip()
        if not ai_criteria:
            return Response(
                {"detail": "This collection has no AI criteria configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def _enqueue():
            from CEBackend.celery import app as celery_app

            celery_app.send_task(
                "quickstart.tasks.business_tasks.reclassify_automated_collection_task",
                args=[collection.pk],
            )

        transaction.on_commit(_enqueue)
        logger.info(
            "Queued reclassify_automated_collection_task for collection pk=%s slug=%s",
            collection.pk,
            collection.slug,
        )
        return Response(
            {
                "status": "queued",
                "collection_id": collection.pk,
                "slug": collection.slug,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        doomed_slug = instance.slug
        response = super().destroy(request, *args, **kwargs)
        if response.status_code == status.HTTP_204_NO_CONTENT:
            trigger_nextjs_revalidation(path="/")
            trigger_nextjs_revalidation(tag="homepage-content")
            trigger_nextjs_revalidation(tag="collections")
            self._invalidate_collection_caches(collection_slug=doomed_slug)
            logger.info("Deleted collection and triggered revalidation + prewarm")
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
            trigger_nextjs_revalidation(tag="collections")
            self._invalidate_collection_caches()
            return Response({"status": "success"})
        except Exception as e:
            logger.error(f"Failed to update collection order: {e}")
            return Response({"error": "Internal error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=["get"], url_path="children")
    def children(self, request, pk=None):
        """List sub-collections for a top-level collection (admin)."""
        parent = self.get_object()
        if parent.parent_id:
            return Response(
                {"detail": "Only top-level collections have sub-collections."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = (
            ClassCollection.objects.filter(parent=parent)
            .annotate(class_count=Count("classes", distinct=True))
            .order_by("sort_order", "name")
        )
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="bulk-assign-classes")
    def bulk_assign_classes(self, request, pk=None):
        """Add this collection to many classes (M2M). Body: { \"class_ids\": [1, 2, 3] }."""
        collection = self.get_object()
        raw_ids = request.data.get("class_ids")
        if not isinstance(raw_ids, list):
            return Response(
                {"error": "class_ids must be a list of integers"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ids = []
        for x in raw_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            return Response(
                {"error": "No valid class IDs"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            classes = list(ClassesMain.objects.filter(classId__in=ids))
            for klass in classes:
                klass.collections.add(collection)
        self._invalidate_collection_caches(collection_slug=collection.slug)
        trigger_nextjs_revalidation(path="/")
        trigger_nextjs_revalidation(tag="homepage-content")
        trigger_nextjs_revalidation(tag="collections")
        return Response({"added": len(classes), "requested": len(ids)})


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

        business_id = self.request.query_params.get("business_id")
        if business_id:
            try:
                bid = int(business_id)
                queryset = queryset.filter(
                    Q(classId__businessId_id=bid) | Q(businessId_id=bid)
                )
            except (TypeError, ValueError):
                pass

        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(
                page, many=True, context={"request": request}
            )
            response = self.get_paginated_response(serializer.data)
        else:
            serializer = self.get_serializer(
                queryset, many=True, context={"request": request}
            )
            response = Response(serializer.data)

        if str(request.query_params.get("include_google", "")).lower() in (
            "1",
            "true",
            "yes",
        ):
            st = request.query_params.get("status")
            if st and st not in ("all", ""):
                if st != "approved":
                    if isinstance(response.data, dict):
                        response.data["google_reviews"] = []
                    return response
            rep = request.query_params.get("reported")
            if str(rep).lower() in ("true", "1", "yes"):
                if isinstance(response.data, dict):
                    response.data["google_reviews"] = []
                return response

            g_qs = ImportedGoogleReview.objects.select_related("business").order_by(
                "-review_date"
            )
            search_term = (request.query_params.get("search") or "").strip()
            if search_term:
                g_qs = g_qs.filter(
                    Q(reviewer_name__icontains=search_term)
                    | Q(comment__icontains=search_term)
                    | Q(business__businessName__icontains=search_term)
                )
            business_id = request.query_params.get("business_id")
            if business_id:
                try:
                    g_qs = g_qs.filter(business_id=int(business_id))
                except (TypeError, ValueError):
                    pass
            rating_filter = request.query_params.get("rating")
            if rating_filter and str(rating_filter).isdigit():
                g_qs = g_qs.filter(rating=int(rating_filter))
            g_qs = g_qs[:300]
            payload = ImportedGoogleReviewSerializer(g_qs, many=True).data
            if isinstance(response.data, dict):
                response.data["google_reviews"] = payload

        return response

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
