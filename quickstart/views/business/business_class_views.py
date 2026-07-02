from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from rest_framework import viewsets, status, filters, generics, permissions
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import (
    PermissionDenied,
    ValidationError as DRFValidationError,
    NotFound,
)  # Added NotFound
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from django.db import transaction
from django.db.models import (
    Q,
    Prefetch,
    Count,
    Avg,
    OuterRef,
    Subquery,
    IntegerField,
    F,
    Sum,
    Value,
    DecimalField,
    Exists,
    Case,
    When,
    ExpressionWrapper,
)
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from django.core.files.storage import default_storage
import logging
import json
from django.utils import timezone
from rest_framework.permissions import AllowAny

from quickstart.serializers.admin.class_management.class_management_serializers import (
    AdminClassCategorySerializer,
)
from quickstart.utils.revalidation import trigger_nextjs_revalidation
from quickstart.views.public.public_business_views import invalidate_business_detail_cache
from quickstart.views.public.public_class_views import invalidate_class_detail_cache
from quickstart.utils.email_utils import send_booking_cancelled_by_other_email
from quickstart.utils.sms_utils import normalize_phone_for_sns, business_sms_enabled
from quickstart.tasks.notification_tasks import send_sms_task

from quickstart.models import (
    BusinessInfo,
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    ImportedGoogleReview,
)

from quickstart.serializers import (
    ManagedClassSerializer,
    ClassCreateSerializer,
    ClassImageSerializer,
    ManagedClassOptionSerializer,
    ScheduleSerializer,
    ScheduleInstanceSerializer,
    BulkScheduleCreateSerializer,
    PublicCategorySerializer,
    PublicSubcategorySerializer,
    BusinessContactInfoSerializer,
    ScheduleGroupActionSerializer,
)

from quickstart.utils.permissions import (
    CanManageOwnClasses,
    CanManageOwnClassesOrClassAdmin,
    IsVerifiedAndActiveBusinessMember,
)


logger = logging.getLogger(__name__)


def _user_is_class_schedule_admin(user):
    return bool(
        user
        and user.is_authenticated
        and user.has_perm("quickstart.access_class_admin")
    )


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 12
    page_size_query_param = "page_size"
    max_page_size = 48


# --- Business ViewSet for Managing Classes ---
class PublicCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a list of ALL public class categories. For each category,
    it only includes subcategories that contain at least one class.
    Cached long-term (30 days); categories rarely change. Invalidated by
    admin category create/update/delete via Next.js revalidateTag("categories").
    """

    permission_classes = [AllowAny]
    serializer_class = PublicCategorySerializer

    def get_queryset(self):
        """Categories removed from platform; return empty so clients don't break."""
        return ClassCategory.objects.none()

    # Cache for 30 days so explore page categories load instantly; admin revalidates when needed
    @method_decorator(cache_page(60 * 60 * 24 * 30))  # 30 days
    @method_decorator(vary_on_headers("Authorization"))
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


# --- NEW: ViewSet to provide ALL categories for business-side forms ---
class AllCategoriesForBusinessViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a complete, unpaginated list of all categories and their subcategories.
    This is specifically for use in business-facing forms.
    MODIFIED: This endpoint is now cached for 15 minutes for performance.
    """

    permission_classes = [AllowAny]
    serializer_class = AdminClassCategorySerializer
    pagination_class = None

    def get_queryset(self):
        """Categories removed from platform; return empty for business forms."""
        return ClassCategory.objects.none()

    # --- ADDED: Caching decorator for the list view ---
    @method_decorator(cache_page(60 * 15))  # Cache for 15 minutes
    @method_decorator(vary_on_headers("Authorization"))
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


class BusinessClassViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Business Users to manage their own ClassesMain.
    Handles CRUD, image management, and status toggling.
    MODIFIED to handle Multi-Tier Options (Create/Update/Delete).
    """

    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
    ]
    parser_classes = [JSONParser, FormParser, MultiPartParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    pagination_class = StandardResultsSetPagination

    search_fields = [
        "title",
        "description",
        "options__title",
        "status",
    ]
    ordering_fields = [
        "title",
        "createdAt",
        "updatedAt",
        "status",
        "average_rating",
        "review_count",
    ]
    ordering = ["-updatedAt"]

    # --- Subqueries for Review Counts ---
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef("pk"))
        .values("classId")
        .annotate(count=Count("reviewId"))
        .values("count")[:1],
        output_field=IntegerField(),
    )
    GOOGLE_REVIEW_COUNT_SUBQUERY = Subquery(
        ImportedGoogleReview.objects.filter(business=OuterRef("businessId"))
        .values("business")
        .annotate(count=Count("id"))
        .values("count")[:1],
        output_field=IntegerField(),
    )

    # --- Subqueries for Review Score Sums ---
    NATIVE_RATING_SUM_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef("pk"))
        .values("classId")
        .annotate(total=Sum("rating"))
        .values("total")[:1],
        output_field=IntegerField(),
    )
    GOOGLE_RATING_SUM_SUBQUERY = Subquery(
        ImportedGoogleReview.objects.filter(business=OuterRef("businessId"))
        .values("business")
        .annotate(total=Sum("rating"))
        .values("total")[:1],
        output_field=IntegerField(),
    )

    def get_serializer_class(self):
        if self.action == "create":
            return ClassCreateSerializer
        return ManagedClassSerializer

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = self.request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        ctx["business"] = business
        return ctx

    def get_queryset(self):
        """Filter queryset to only classes belonging to the user's associated business."""
        user = self.request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            logger.warning(
                f"User {user.email} lacks associated business for BusinessClassViewSet."
            )
            return ClassesMain.objects.none()

        return (
            ClassesMain.objects.filter(businessId=business)
            .exclude(status="suspended")
            .select_related("businessId", "location_ref")
            .prefetch_related(
                Prefetch(
                    "images",
                    queryset=ClassImage.objects.order_by("-isCover", "createdAt"),
                ),
                Prefetch(
                    "options",
                    queryset=ClassOption.objects.order_by("optionId"),
                ),
            )
            .annotate(
                native_review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
                google_review_count=Coalesce(
                    self.GOOGLE_REVIEW_COUNT_SUBQUERY, Value(0)
                ),
                native_rating_sum=Coalesce(self.NATIVE_RATING_SUM_SUBQUERY, Value(0)),
                google_rating_sum=Coalesce(self.GOOGLE_RATING_SUM_SUBQUERY, Value(0)),
                last_schedule_date=Subquery(
                    ScheduleInstance.objects.filter(
                        schedule__option__classId=OuterRef("pk"),
                        date__gte=timezone.now().date(),
                    )
                    .order_by("-date")
                    .values("date")[:1]
                ),
            )
            .annotate(
                review_count=F("native_review_count") + F("google_review_count"),
                total_rating_sum=F("native_rating_sum") + F("google_rating_sum"),
            )
            .annotate(
                average_rating=Case(
                    When(
                        review_count__gt=0,
                        then=ExpressionWrapper(
                            F("total_rating_sum") * 1.0 / F("review_count"),
                            output_field=DecimalField(max_digits=3, decimal_places=1),
                        ),
                    ),
                    default=Value(Decimal("0.0")),
                    output_field=DecimalField(max_digits=3, decimal_places=1),
                )
            )
            .distinct()
        )

    def _trigger_class_revalidation(self, class_instance, old_slug=None):
        """Helper to trigger all necessary revalidations for a class."""
        if not class_instance:
            return

        # 1. Revalidate by TAG (this is what actually works for cached pages)
        if hasattr(class_instance, "slug") and class_instance.slug:
            trigger_nextjs_revalidation(tag=f"class-{class_instance.slug}")
            invalidate_class_detail_cache(
                slug=class_instance.slug, class_id=class_instance.classId
            )

        if old_slug and old_slug != getattr(class_instance, "slug", None):
            trigger_nextjs_revalidation(tag=f"class-{old_slug}")
            trigger_nextjs_revalidation(path=f"/classes/{old_slug}")
            invalidate_class_detail_cache(slug=old_slug)

        # 2. Also revalidate by class ID tag (if your fetcher uses it)
        trigger_nextjs_revalidation(tag=f"class-{class_instance.classId}")

        # 3. Revalidate the homepage to update the "Find a Class" section
        trigger_nextjs_revalidation(path="/")

        # 4. Revalidate the business page (tag + path) and invalidate Django API cache so cover/classes refresh
        if class_instance.businessId and hasattr(class_instance.businessId, "slug"):
            business_slug = class_instance.businessId.slug
            invalidate_business_detail_cache(business_slug)
            trigger_nextjs_revalidation(tag=f"business-{business_slug}")
            trigger_nextjs_revalidation(path=f"/business/{business_slug}")
            logger.info("Revalidated business page: business-%s and path /business/%s", business_slug, business_slug)

        # 5. Revalidate cache tags to update explore/search pages
        tags_to_revalidate = ["classes-search", "homepage-classes", "classes"]

        for tag in tags_to_revalidate:
            trigger_nextjs_revalidation(tag=tag)

        logger.info(
            f"Triggered revalidation for class {class_instance.pk} (slug: {class_instance.slug}) and related tags."
        )

    def perform_create(self, serializer):
        """Associate the new class with the user's business and handle images/options from S3 keys."""
        user = self.request.user
        request_data = self.request.data
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            raise PermissionDenied(
                "You must be associated with a business to create a class."
            )

        instance = None
        try:
            with transaction.atomic():
                instance = serializer.save(
                    businessId=business,
                    status="active",
                    city=request_data.get("city"),
                    state=request_data.get("state"),
                )
                logger.info(
                    f"Class '{instance.title}' (ID: {instance.classId}) created for business '{business.businessName}' by user {user.email}"
                )
                self._process_images_and_options_on_create(instance, request_data)

            # --- MODIFIED: Trigger revalidation after successful creation ---
            self._trigger_class_revalidation(instance)

        except DRFValidationError as e:
            logger.warning(
                f"Validation error during class creation by {user.email}: {e.detail}"
            )
            raise
        except Exception as e:
            if instance and instance.pk:
                instance.delete()
            logger.error(
                f"Error during perform_create for class by {user.email}: {e}",
                exc_info=True,
            )
            raise DRFValidationError(
                {
                    "detail": f"An unexpected error occurred during class creation: {str(e)}"
                }
            )

    def _process_images_and_options_on_create(self, class_instance, request_data):
        """Helper to handle images (from S3 keys) and options (List) during creation."""
        # 1. Image Processing
        image_s3_keys = request_data.get("image_s3_keys", [])
        cover_image_s3_key = request_data.get("cover_image_s3_key")

        _min_images = 4
        if not image_s3_keys or len(image_s3_keys) < _min_images:
            raise DRFValidationError(
                {
                    "image_s3_keys": f"At least {_min_images} class images are required.",
                }
            )

        if cover_image_s3_key and cover_image_s3_key not in image_s3_keys:
            raise DRFValidationError(
                {
                    "cover_image_s3_key": "The selected cover image key must be one of the uploaded image keys."
                }
            )

        if not cover_image_s3_key and image_s3_keys:
            cover_image_s3_key = image_s3_keys[0]

        image_objects_to_create = [
            ClassImage(
                classId=class_instance,
                image=key,
                isCover=(key == cover_image_s3_key),
            )
            for key in image_s3_keys
        ]
        ClassImage.objects.bulk_create(image_objects_to_create)
        logger.info(
            f"Bulk-created {len(image_objects_to_create)} images for class {class_instance.classId} from S3 keys."
        )

        # 2. Options Processing (MULTI-TIER SUPPORT)
        options_json_string = request_data.get("options")
        if not options_json_string:
            raise DRFValidationError({"options": "Class option data is required."})

        try:
            options_data_list = json.loads(options_json_string)
            if not (isinstance(options_data_list, list) and len(options_data_list) > 0):
                raise DRFValidationError(
                    {
                        "options": "Options data must be a list containing at least one option object."
                    }
                )

            # Loop through all options in the array
            for index, option_dict in enumerate(options_data_list):
                # Enforce that the first option (index 0) is always primary
                # unless explicitly set, but for new classes, first is safe assumption for primary.
                if index == 0:
                    option_dict["schedule_mode"] = "primary"
                
                # If subsequent items don't have a schedule_mode, default to synced
                if "schedule_mode" not in option_dict:
                    option_dict["schedule_mode"] = "synced"

                option_serializer = ManagedClassOptionSerializer(data=option_dict)
                option_serializer.is_valid(raise_exception=True)
                option_serializer.save(classId=class_instance)
            
            logger.info(f"Created {len(options_data_list)} ClassOptions for class {class_instance.classId}.")

        except json.JSONDecodeError:
            raise DRFValidationError(
                {"options": "Invalid JSON format for options data."}
            )
        except Exception as e:
            logger.error(
                f"Error creating ClassOption for class {class_instance.classId}: {str(e)}",
                exc_info=True,
            )
            raise DRFValidationError(
                {"options": f"Failed to create class option: {str(e)}"}
            )

    def perform_update(self, serializer):
        """
        Handles updates for a class, its images, and options (Smart Sync), then triggers revalidation.
        """
        instance = serializer.instance
        user = self.request.user
        request_data = self.request.data

        with transaction.atomic():
            old_slug = instance.slug
            updated_instance = serializer.save()
            logger.info(
                f"Class '{updated_instance.title}' (ID: {updated_instance.pk}) base fields updated by user {user.email}"
            )

            # 1. Image Deletion
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
                            f"Deleted {deleted_count} ClassImage records for class {instance.pk}."
                        )
            except json.JSONDecodeError:
                logger.warning(
                    f"Could not parse delete_image_ids: {delete_image_ids_str}"
                )

            # 2. Image Addition
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
                        f"Bulk-added {len(img_objects)} new images for class {instance.pk} from S3 keys."
                    )
            except json.JSONDecodeError:
                logger.warning(
                    f"Could not parse new_image_s3_keys: {new_image_s3_keys_str}"
                )

            # 3. Cover Image Update
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
                raise DRFValidationError(
                    {
                        "images": f"At least {_min_images} class images are required.",
                    }
                )

            # 4. Multi-Tier Options Update (Smart Sync)
            options_json_string = request_data.get("options")
            if options_json_string:
                try:
                    options_data_list = json.loads(options_json_string)
                    if not isinstance(options_data_list, list):
                         raise DRFValidationError({"options": "Options data must be a list."})

                    # A. Identify IDs present in the payload (Existing Options)
                    incoming_ids = [
                        item.get("optionId") 
                        for item in options_data_list 
                        if item.get("optionId")
                    ]

                    # B. Delete Options NOT in the payload (User deleted them in UI)
                    if len(options_data_list) > 0:
                        ClassOption.objects.filter(classId=instance).exclude(
                            optionId__in=incoming_ids
                        ).delete()

                    # C. Update or Create Options
                    for index, option_dict in enumerate(options_data_list):
                        option_id = option_dict.get("optionId")

                        if index == 0:
                             option_dict["schedule_mode"] = "primary"

                        if option_id:
                            # Update existing option
                            option_instance = get_object_or_404(
                                ClassOption, optionId=option_id, classId=instance
                            )
                            option_serializer = ManagedClassOptionSerializer(
                                option_instance, data=option_dict, partial=True
                            )
                        else:
                            # Create new option
                            option_serializer = ManagedClassOptionSerializer(data=option_dict)

                        option_serializer.is_valid(raise_exception=True)
                        option_serializer.save(classId=instance) 

                except Exception as e:
                    logger.error(
                        f"Error processing options update for class {instance.pk}: {e}",
                        exc_info=True,
                    )
                    raise DRFValidationError(
                        {"options": f"Failed to update class options: {str(e)}"}
                    )

        # --- Trigger revalidation for the updated class ---
        self._trigger_class_revalidation(
            updated_instance,
            old_slug=old_slug if old_slug != updated_instance.slug else None,
        )

    def perform_destroy(self, instance):
        # --- Capture instance data before modification ---
        class_to_revalidate = instance

        try:
            with transaction.atomic():
                instance.status = "suspended"
                instance.save(update_fields=["status"])
                logger.info(
                    f"Class '{class_to_revalidate.title}' (ID: {class_to_revalidate.pk}) status set to 'suspended' by business user {self.request.user.email}."
                )

                today = timezone.now().date()
                future_instances_to_delete = ScheduleInstance.objects.filter(
                    schedule__option__classId=instance,
                    date__gte=today,
                    status="scheduled",
                )

                if future_instances_to_delete.exists():
                    bookings_to_cancel = Booking.objects.filter(
                        schedule_instance__in=future_instances_to_delete,
                        status="confirmed",
                    ).select_related("user")

                    cancellation_reason = f"The class '{class_to_revalidate.title}' is no longer available."
                    contact_info = settings.NOTIFICATION_SETTINGS.get(
                        "reply_to", "support@classeasily.com"
                    )

                    business = class_to_revalidate.businessId
                    for booking in bookings_to_cancel:
                        try:
                            send_booking_cancelled_by_other_email(
                                user=booking.user,
                                booking=booking,
                                cancelled_by="the business",
                                reason=cancellation_reason,
                                contact_info=contact_info,
                            )
                        except Exception as email_error:
                            logger.error(
                                f"Failed to send class suspension cancellation email for booking {booking.id}: {email_error}",
                                exc_info=True,
                            )
                        if business_sms_enabled(business) and booking.schedule_instance:
                            user_to_notify = booking.user or booking.contact
                            if user_to_notify:
                                phone = getattr(user_to_notify, "phone_number", None) or (booking.metadata or {}).get("guest_phone") or ""
                                normalized = normalize_phone_for_sns(phone)
                                if normalized:
                                    class_title = getattr(class_to_revalidate, "title", "Class")
                                    date_str = booking.schedule_instance.date.strftime("%b %d")
                                    t = booking.schedule_instance.time
                                    time_str = t.strftime("%I:%M %p").lstrip("0") if hasattr(t, "strftime") else str(t)
                                    business_name = getattr(business, "businessName", "") or "ClassEasily"
                                    sms_msg = f"Your booking for {class_title} on {date_str} at {time_str} has been cancelled.\n\nIf you paid, you'll receive a refund.\n\n— {business_name}"
                                    try:
                                        send_sms_task.delay(normalized, sms_msg)
                                    except Exception as sms_e:
                                        logger.warning("Cancellation SMS failed for booking %s: %s", booking.id, sms_e)

                    deleted_count = 0
                    for instance_to_delete in future_instances_to_delete:
                        instance_to_delete.delete()
                        deleted_count += 1
                    logger.info(
                        f"Successfully deleted {deleted_count} future schedule instances for suspended class '{instance.title}' (ID: {instance.pk})."
                    )
                else:
                    logger.info(
                        f"No active future schedule instances found to delete for class '{class_to_revalidate.title}' (ID: {class_to_revalidate.pk})."
                    )

            # --- Trigger revalidation after suspension ---
            self._trigger_class_revalidation(class_to_revalidate)

        except Exception as e:
            logger.error(
                f"Error during deactivation and cleanup for class {class_to_revalidate.pk}: {str(e)}",
                exc_info=True,
            )
            raise DRFValidationError(
                f"Could not deactivate the class and its schedules due to an error: {str(e)}"
            )

    @action(detail=False, methods=["get"], url_path="contact-info")
    def contact_info(self, request, *args, **kwargs):
        user = request.user
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            raise NotFound("No active business profile found for this user.")

        serializer = BusinessContactInfoSerializer(business)
        return Response(serializer.data)

    @action(detail=True, methods=["delete"], url_path="images/(?P<image_id>[^/.]+)")
    def delete_image(self, request, pk=None, image_id=None):
        class_instance = self.get_object()
        try:
            image = get_object_or_404(
                ClassImage, imageId=image_id, classId=class_instance
            )
            if image.image:
                try:
                    image.image.delete(save=False)
                except Exception as e:
                    logger.warning(
                        f"Could not delete S3 file for image {image_id}: {e}"
                    )

            image_id_log = image.imageId
            image.delete()
            logger.info(
                f"Image ID {image_id_log} deleted from class '{class_instance.title}' by {request.user.email}"
            )
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Http404:
            raise NotFound("Image not found for this class.")
        except Exception as e:
            logger.error(
                f"Error deleting image {image_id} for class {pk}: {e}", exc_info=True
            )
            raise DRFValidationError({"error": f"Failed to delete image: {e}"})

    @action(detail=True, methods=["patch"], url_path="toggle-active")
    def toggle_class_active(self, request, pk=None):
        """Toggle class active/inactive status and trigger revalidation."""
        class_instance = self.get_object()
        if class_instance.status == "suspended":
            raise PermissionDenied(
                "Suspended classes cannot be toggled. Contact support."
            )

        new_status = "inactive" if class_instance.status == "active" else "active"
        old_status = class_instance.status

        class_instance.status = new_status
        class_instance.save(update_fields=["status"])
        logger.info(
            f"Class '{class_instance.title}' status toggled from {old_status} to {new_status} by {request.user.email}"
        )

        # --- Trigger revalidation after toggling status ---
        self._trigger_class_revalidation(class_instance)

        return Response({"status": class_instance.status})
# --- Views for related models (Options, Schedules, Instances, Breaks) ---


class BusinessClassOptionDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ManagedClassOptionSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
    ]
    lookup_url_kwarg = "option_id"
    lookup_field = "optionId"  # Match model field

    def get_queryset(self):
        user = self.request.user
        class_pk = self.kwargs.get("pk")
        business = BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        ).first()
        if not business:
            return ClassOption.objects.none()
        # Ensure the class_pk belongs to the business as well
        return ClassOption.objects.filter(
            classId_id=class_pk, classId__businessId=business
        )

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        # Use the correct field for lookup based on lookup_field
        filter_kwargs = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        obj = get_object_or_404(queryset, **filter_kwargs)
        # Permission check is implicitly handled by get_queryset filtering by business
        # self.check_object_permissions(self.request, obj.classId) # Not strictly needed if queryset is correct
        return obj

    def perform_update(self, serializer):
        serializer.validated_data.pop("classId", None)  # Don't change parent class
        instance = serializer.save()
        logger.info(
            f"ClassOption ID {instance.optionId} updated by {self.request.user.email}"
        )

    def perform_destroy(self, instance):
        option_id = instance.optionId
        # Check for confirmed bookings across ALL schedules linked to this option.
        if Booking.objects.filter(
            schedule_instance__schedule__option=instance, status="confirmed"
        ).exists():
            raise PermissionDenied(
                "Cannot delete this option because one of its schedules has confirmed bookings. Please cancel the schedules first."
            )
        # If no bookings, proceed with deletion. The CASCADE will handle deleting schedules.
        instance.delete()
        logger.info(f"ClassOption ID {option_id} deleted by {self.request.user.email}")


class BusinessScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClassesOrClassAdmin,
    ]

    def get_queryset(self):
        user = self.request.user
        is_class_admin = _user_is_class_schedule_admin(user)

        if is_class_admin:
            queryset = Schedule.objects.all()
        else:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                return Schedule.objects.none()
            queryset = Schedule.objects.filter(option__classId__businessId=business)

        option_id = self.request.query_params.get("option_id")
        if option_id and option_id.isdigit():
            queryset = queryset.filter(option_id=int(option_id))

        booked_participants_subquery = (
            Booking.objects.filter(
                schedule_instance__schedule=OuterRef("pk"), status="confirmed"
            )
            .values("schedule_instance__schedule")
            .annotate(total_pax=Sum("participants"))
            .values("total_pax")
        )

        total_revenue_subquery = (
            Booking.objects.filter(
                schedule_instance__schedule=OuterRef("pk"),
                status="confirmed",
                payment_status="paid",
            )
            .values("schedule_instance__schedule")
            .annotate(total_rev=Sum("amount_paid"))
            .values("total_rev")
        )

        has_bookings_subquery = Booking.objects.filter(
            schedule_instance__schedule=OuterRef("pk"), status="confirmed"
        )

        queryset = (
            queryset.select_related("option", "option__classId")
            .annotate(
                booked_participants=Coalesce(
                    Subquery(booked_participants_subquery, output_field=IntegerField()),
                    Value(0),
                ),
                total_revenue=Coalesce(
                    Subquery(total_revenue_subquery, output_field=DecimalField()),
                    Value(Decimal("0.00")),
                ),
                has_confirmed_bookings=Exists(has_bookings_subquery),
            )
            .order_by("option__classId__title", "day", "time")
        )

        return queryset

    def perform_create(self, serializer):
        option = serializer.validated_data.get("option")
        user = self.request.user
        if _user_is_class_schedule_admin(user):
            if not option:
                raise PermissionDenied("Option is required.")
        else:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business or not option or option.classId.businessId != business:
                raise PermissionDenied(
                    "Cannot create schedule for an option not belonging to your business."
                )

        schedule = serializer.save()

        # FIX: Explicitly create the ScheduleInstance(s) for the new Schedule.
        if schedule.option.booking_type == "Full Course":
            # This method on the model generates all instances for a course.
            schedule.generate_course_instances()
            logger.info(
                f"Course Schedule and its instances created for Option ID {option.optionId} by {user.email}"
            )
        elif schedule.date:
            # For single sessions, create one instance.
            ScheduleInstance.objects.create(
                schedule=schedule,
                date=schedule.date,
                time=schedule.time,
                duration=schedule.duration,
                price=schedule.price,
                max_participants=schedule.maxParticipants,
                min_participants=schedule.minParticipants,
                status="scheduled",
            )
            logger.info(
                f"Single Session Schedule and its instance created for Option ID {option.optionId} by {user.email}"
            )

        # --- ADDED: Trigger revalidation for the class page ---
        class_obj = option.classId
        trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
        logger.info(f"Revalidated class page: /classes/{class_obj.slug}")

    @action(detail=False, methods=["post"], url_path="bulk-create")
    def bulk_create(self, request, *args, **kwargs):
        """
        Bulk creates multiple independent Schedule objects, skipping any conflicts.
        """
        serializer = BulkScheduleCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        option = data["option"]
        start_date = data["start_date"]
        end_date = data["end_date"]
        days_of_week_map = {
            "Mon": 0,
            "Tue": 1,
            "Wed": 2,
            "Thu": 3,
            "Fri": 4,
            "Sat": 5,
            "Sun": 6,
        }
        target_weekdays = {days_of_week_map[day] for day in data["days_of_week"]}

        schedules_to_create = []
        skipped_count = 0

        # Fetch existing schedules in the date range for conflict checking
        existing_schedules = Schedule.objects.filter(
            option=option, date__range=[start_date, end_date]
        ).values_list("date", "time")

        # Use a set for efficient conflict lookups
        existing_schedule_set = {(date, time) for date, time in existing_schedules}

        current_date = start_date
        while current_date <= end_date:
            if current_date.weekday() in target_weekdays:
                for time_val in data["times"]:
                    # CONFLICT CHECK
                    if (current_date, time_val) in existing_schedule_set:
                        skipped_count += 1
                        continue  # Skip this conflicting schedule

                    schedules_to_create.append(
                        Schedule(
                            name=data.get("name"),
                            option=option,
                            date=current_date,
                            day=current_date.strftime("%a"),
                            time=time_val,
                            duration=data["duration"],
                            price=data["price"],
                            maxParticipants=data["maxParticipants"],
                            minParticipants=data["minParticipants"],
                        )
                    )
            current_date += timedelta(days=1)

        if not schedules_to_create:
            message = "No new schedules were created."
            if skipped_count > 0:
                message += f" {skipped_count} conflicting schedule(s) were skipped."
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                created_schedules = Schedule.objects.bulk_create(schedules_to_create)

                # Manually generate ScheduleInstance for each created Schedule
                instances_to_create = [
                    ScheduleInstance(
                        schedule=schedule,
                        date=schedule.date,
                        time=schedule.time,
                        duration=schedule.duration,
                        price=schedule.price,
                        max_participants=schedule.maxParticipants,
                        min_participants=schedule.minParticipants,
                        status="scheduled",
                    )
                    for schedule in created_schedules
                ]
                ScheduleInstance.objects.bulk_create(instances_to_create)

            logger.info(
                f"Bulk created {len(created_schedules)} schedules for option {option.pk} by {request.user.email} ({skipped_count} skipped)."
            )

            # --- ADDED: Trigger revalidation for the class page ---
            class_obj = option.classId
            trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
            logger.info(
                f"Revalidated class page after bulk schedule creation: /classes/{class_obj.slug}"
            )

            response_serializer = self.get_serializer(created_schedules, many=True)
            return Response(
                {
                    "message": f"Successfully created {len(created_schedules)} schedules. {skipped_count} were skipped due to conflicts.",
                    "created_count": len(created_schedules),
                    "skipped_count": skipped_count,
                    "data": response_serializer.data,
                },
                status=status.HTTP_201_CREATED,
            )

        except Exception as e:
            logger.error(
                f"Error during schedule bulk creation: {str(e)}", exc_info=True
            )
            return Response(
                {"detail": f"An error occurred: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["post"], url_path="group-update")
    def group_update(self, request, *args, **kwargs):
        """
        Updates a group of schedules identified by name and option_id.
        Updates fields like time, price, duration, capacity.
        """
        user = request.user
        is_class_admin = _user_is_class_schedule_admin(user)
        if not is_class_admin:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                raise PermissionDenied("User is not associated with any business.")

        option_id = request.data.get("option_id")
        group_name = request.data.get("name")
        updates = request.data.get("updates", {})

        if not option_id or not group_name:
            return Response(
                {"detail": "option_id and name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if is_class_admin:
                option = ClassOption.objects.get(pk=option_id)
            else:
                option = ClassOption.objects.get(pk=option_id, classId__businessId=business)
        except ClassOption.DoesNotExist:
            raise NotFound("Class option not found or access denied.")

        # Find schedules
        schedules_to_update = Schedule.objects.filter(option=option, name=group_name)
        
        if not schedules_to_update.exists():
            return Response(
                {"detail": "No schedules found for this group."},
                status=status.HTTP_404_NOT_FOUND,
            )

        updated_count = 0
        
        try:
            with transaction.atomic():
                for schedule in schedules_to_update:
                    # Apply updates
                    has_changes = False
                    
                    if "time" in updates:
                        schedule.time = updates["time"]
                        has_changes = True
                    if "duration" in updates:
                        schedule.duration = updates["duration"]
                        has_changes = True
                    if "price" in updates:
                        p = Decimal(str(updates["price"]))
                        if p <= 0:
                            return Response(
                                {"detail": "Price must be greater than zero."},
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                        schedule.price = updates["price"]
                        has_changes = True
                    if "maxParticipants" in updates:
                        schedule.maxParticipants = updates["maxParticipants"]
                        has_changes = True
                    if "minParticipants" in updates:
                        schedule.minParticipants = updates["minParticipants"]
                        has_changes = True
                    
                    # Only save if changed (this triggers the model's save() signal 
                    # which handles updating ScheduleInstance logic)
                    if has_changes:
                        schedule.save()
                        updated_count += 1
            
            # Trigger Next.js Revalidation
            class_obj = option.classId
            trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
            
            return Response({
                "message": f"Successfully updated {updated_count} schedules in group '{group_name}'.",
                "updated_count": updated_count
            })

        except Exception as e:
            logger.error(f"Error updating schedule group: {e}", exc_info=True)
            return Response(
                {"detail": f"Failed to update group: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
    @action(detail=False, methods=["post"], url_path="group-delete")
    def group_delete(self, request, *args, **kwargs):
        """
        Deletes a whole group of schedules identified by name and option_id.
        Fails if any schedule in the group has confirmed bookings.
        """
        user = request.user
        is_class_admin = _user_is_class_schedule_admin(user)
        if not is_class_admin:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                raise PermissionDenied("User is not associated with any business.")

        serializer = ScheduleGroupActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        option = serializer.validated_data["option_id"]
        group_name = serializer.validated_data["name"]

        if not is_class_admin and option.classId.businessId != business:
            raise PermissionDenied(
                "You do not have permission to access this class option."
            )

        schedules_to_delete = Schedule.objects.filter(option=option, name=group_name)

        if not schedules_to_delete.exists():
            return Response(
                {"detail": "No schedules found for this group."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Critical check: do not allow deletion if any schedule has confirmed bookings.
        if Booking.objects.filter(
            schedule_instance__schedule__in=schedules_to_delete, status="confirmed"
        ).exists():
            raise PermissionDenied(
                "Cannot delete group: one or more schedules have confirmed bookings."
            )

        # Store class info before deletion
        class_obj = option.classId

        deleted_count, _ = schedules_to_delete.delete()

        logger.info(
            f"User {user.email} deleted schedule group '{group_name}' ({deleted_count} schedules) for option {option.pk}."
        )

        # --- ADDED: Trigger revalidation for the class page ---
        trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
        logger.info(
            f"Revalidated class page after group deletion: /classes/{class_obj.slug}"
        )

        return Response(
            {
                "message": f"Successfully deleted {deleted_count} schedules in group '{group_name}'."
            },
            status=status.HTTP_200_OK,
        )

    def perform_update(self, serializer):
        instance = self.get_object()
        serializer.validated_data.pop("option", None)
        updated_instance = serializer.save()

        logger.info(f"Schedule ID {instance.pk} updated by {self.request.user.email}")

        # --- ADDED: Trigger revalidation for the class page ---
        option = instance.option
        class_obj = option.classId
        trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
        logger.info(
            f"Revalidated class page after schedule update: /classes/{class_obj.slug}"
        )

    def perform_destroy(self, instance):
        schedule_id = instance.pk
        logger.info(
            f"Business user {self.request.user.email} initiating deletion of Schedule ID {schedule_id}."
        )

        # Check for confirmed bookings across all instances of this schedule
        if Booking.objects.filter(
            schedule_instance__schedule=instance, status="confirmed"
        ).exists():
            logger.warning(
                f"Deletion of Schedule ID {schedule_id} blocked due to existing confirmed bookings."
            )
            raise PermissionDenied(
                "Cannot delete this schedule as it has confirmed bookings. "
                "Please cancel or reassign bookings first, or cancel individual future sessions."
            )

        # Store class info before deletion
        option = instance.option
        class_obj = option.classId

        # If no confirmed bookings, proceed with deletion (which cascades to instances)
        instance.delete()
        logger.info(
            f"Schedule ID {schedule_id} deleted by {self.request.user.email} (hard delete executed)."
        )

        # --- ADDED: Trigger revalidation for the class page ---
        trigger_nextjs_revalidation(path=f"/classes/{class_obj.slug}")
        logger.info(
            f"Revalidated class page after schedule deletion: /classes/{class_obj.slug}"
        )


class BusinessScheduleInstanceViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleInstanceSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClassesOrClassAdmin,
    ]
    http_method_names = ["get", "post", "patch", "head", "options"]  # No PUT/DELETE

    def get_queryset(self):
        user = self.request.user
        is_class_admin = _user_is_class_schedule_admin(user)

        if is_class_admin:
            base_filter = ScheduleInstance.objects.all()
        else:
            business = BusinessInfo.objects.filter(
                Q(owner=user)
                | Q(staff_members__user=user, staff_members__status="accepted")
            ).first()
            if not business:
                return ScheduleInstance.objects.none()
            base_filter = ScheduleInstance.objects.filter(
                schedule__option__classId__businessId=business
            )

        queryset = (
            base_filter.select_related("schedule__option__classId")
            .annotate(
                current_bookings_count=Coalesce(
                    Subquery(
                        Booking.objects.filter(
                            schedule_instance=OuterRef("pk"), status="confirmed"
                        )
                        .values("schedule_instance")
                        .annotate(total_pax=Sum("participants"))
                        .values("total_pax")[:1]
                    ),
                    0,
                    output_field=IntegerField(),
                )
            )
        )
        # --- Apply Filters ---
        schedule_id = self.request.query_params.get("schedule_id")
        option_id = self.request.query_params.get("option_id")
        class_id = self.request.query_params.get("class_id")
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        status_filter = self.request.query_params.get("status")

        if schedule_id and schedule_id.isdigit():
            queryset = queryset.filter(schedule_id=schedule_id)
        if option_id and option_id.isdigit():
            queryset = queryset.filter(schedule__option_id=option_id)
        if class_id and class_id.isdigit():
            queryset = queryset.filter(schedule__option__classId_id=class_id)
        if start_date:
            queryset = queryset.filter(date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__lte=end_date)
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        return queryset.order_by("date", "time")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        instance = self.get_object()
        reason = request.data.get("reason", "").strip()
        if not reason:
            raise DRFValidationError({"reason": "Reason required."})
        if instance.status != "scheduled":
            raise DRFValidationError({"status": "Only scheduled can be cancelled."})

        with transaction.atomic():
            instance.status = "cancelled"
            instance.cancellation_reason = reason
            instance.save(update_fields=["status", "cancellation_reason"])
            cancelled_count = Booking.objects.filter(
                schedule_instance=instance, status="confirmed"
            ).update(
                status="cancelled",
                cancelled_at=timezone.now(),
                cancellation_reason=f"Session cancelled: {reason}",
            )
            logger.info(
                f"Cancelled {cancelled_count} bookings for instance {pk} by {request.user.email}"
            )
        # TODO: Notifications
        return Response(self.get_serializer(instance).data)
