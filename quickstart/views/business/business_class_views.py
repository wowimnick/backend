from datetime import timedelta
from decimal import Decimal
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
)  # Added Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.core.files.storage import default_storage
from django.http import Http404
import logging
import json
from django.utils import timezone
from rest_framework.permissions import AllowAny

from quickstart.serializers.admin.class_management.class_management_serializers import (
    AdminClassCategorySerializer,
)

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
)

from quickstart.utils.permissions import (
    CanManageOwnClasses,
    IsVerifiedAndActiveBusinessOwnerOrManager,
)


logger = logging.getLogger(__name__)

# --- Business ViewSet for Managing Classes ---


class PublicCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a list of public class categories that have active classes.
    """

    permission_classes = [AllowAny]
    serializer_class = PublicCategorySerializer

    def get_queryset(self):
        """
        Returns ClassCategory objects that have at least one associated 'active' class,
        where the business is also active and verified.
        """
        active_class_filter = Q(
            classes_in_category__status="active",
            classes_in_category__businessId__isActive=True,
            classes_in_category__businessId__verificationStatus="verified",
        )

        return (
            ClassCategory.objects.annotate(
                active_class_count=Count(
                    "classes_in_category", filter=active_class_filter
                )
            )
            .filter(active_class_count__gt=0)
            .order_by("name")
        )


# --- NEW: ViewSet to provide ALL categories for business-side forms ---
class AllCategoriesForBusinessViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a complete, unpaginated list of all categories and their subcategories.
    This is specifically for use in business-facing forms like registration or class creation,
    where a full, unfiltered list is required for selection.
    """

    permission_classes = [IsAuthenticated]  # User must be logged in to access this
    serializer_class = AdminClassCategorySerializer
    pagination_class = None  # This is the key change to return all results

    def get_queryset(self):
        # We can reuse the same efficient query from the admin panel
        subcat_queryset = ClassSubcategory.objects.annotate(
            class_count=Count("classes_in_subcategory", distinct=True)
        )
        return (
            ClassCategory.objects.annotate(
                class_count=Count("classes_in_category", distinct=True)
            )
            .prefetch_related(Prefetch("subcategories", queryset=subcat_queryset))
            .order_by("name")
        )


class BusinessClassViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Business Users to manage their own ClassesMain.
    Handles CRUD, image management, and status toggling.
    (URL Base: /api/business/classes/)
    """

    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
    ]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]

    search_fields = [
        "title",
        "description",
        "options__title",
        "category__name",
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

    # --- Subqueries for Annotations (Business Context) ---
    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(
            classId=OuterRef("pk")
        )  # No status filter needed? Or 'approved'?
        .values("classId")
        .annotate(avg_rating=Avg("rating"))
        .values("avg_rating")[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1),  # Use DecimalField
    )
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef("pk"))  # No status filter needed?
        .values("classId")
        .annotate(count=Count("reviewId"))
        .values("count")[:1],
        output_field=IntegerField(),
    )

    def get_serializer_class(self):
        if self.action == "create":
            return ClassCreateSerializer
        return ManagedClassSerializer

    def get_queryset(self):
        """Filter queryset to only classes belonging to the user's associated business."""
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()

        if not business:
            logger.warning(
                f"User {user.email} lacks associated business for BusinessClassViewSet."
            )
            return ClassesMain.objects.none()

        # MODIFIED: Removed prefetching of schedules to make this query much lighter.
        # Schedules will now be fetched on-demand by the frontend when needed.
        return (
            ClassesMain.objects.filter(businessId=business)
            .exclude(status="suspended")
            .select_related("businessId", "category", "subcategory")
            .prefetch_related(
                "images",
                # Prefetch options, but NOT their schedules. This is the key change.
                Prefetch(
                    "options",
                    queryset=ClassOption.objects.order_by("optionId"),
                ),
            )
            .annotate(
                average_rating=Coalesce(
                    self.AVERAGE_RATING_SUBQUERY, Value(Decimal("0.0"))
                ),
                review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
            )
            .distinct()
        )

    def perform_create(self, serializer):
        """Associate the new class with the user's business and handle images/options."""
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            raise PermissionDenied(
                "You must be associated with a business to create a class."
            )

        # ADDED: Ensure the business account is active, even if not yet verified.
        # This prevents users with deactivated or suspended business accounts from creating new content.
        if not business.isActive:
            raise PermissionDenied(
                "Your business account is currently inactive. Please contact support to create new classes."
            )

        category_key = serializer.validated_data.pop("category_key")
        subcategory_key = serializer.validated_data.pop("subcategory_key", None)

        try:
            category = ClassCategory.objects.get(key=category_key)
            subcategory = None
            if subcategory_key:
                subcategory = ClassSubcategory.objects.get(
                    category=category, key=subcategory_key
                )

            # A new class is set to 'active' by default, but its public visibility
            # is controlled by the parent business's verification status.
            instance = serializer.save(
                businessId=business,
                category=category,
                subcategory=subcategory,
                status="active",
            )
            logger.info(
                f"Class '{instance.title}' (ID: {instance.classId}) created for business '{business.businessName}' by user {user.email}"
            )
            self._process_images_and_options(instance)

        except ClassCategory.DoesNotExist:
            raise DRFValidationError(
                {"category_key": f"Category '{category_key}' not found."}
            )
        except ClassSubcategory.DoesNotExist:
            raise DRFValidationError(
                {
                    "subcategory_key": f"Subcategory '{subcategory_key}' not found in category '{category_key}'."
                }
            )
        except Exception as e:
            logger.error(
                f"Error during perform_create for class by {user.email}: {e}",
                exc_info=True,
            )
            raise DRFValidationError("An error occurred during class creation.")

    def _process_images_and_options(self, class_instance):
        """Helper method to process images and options from the request."""
        request = self.request

        try:
            # Handle Main Class Images
            images_files = request.FILES.getlist("images")
            if images_files:
                created_image_objects = []
                for index, image_file in enumerate(images_files):
                    created_image_objects.append(
                        ClassImage(
                            classId=class_instance,
                            image=image_file,
                            isCover=(index == 0),
                        )
                    )
                if created_image_objects:
                    ClassImage.objects.bulk_create(created_image_objects)
                    logger.info(
                        f"Bulk uploaded {len(created_image_objects)} images for class {class_instance.classId}. First image set as cover."
                    )
            else:
                logger.warning(
                    f"No main class images provided for class {class_instance.classId}"
                )
                raise DRFValidationError(
                    {"images": "At least 5 class images are required."}
                )

            # Handle Single Class Option from request.data
            options_json_string = request.data.get("options")
            if options_json_string:
                logger.info(
                    f"Received options JSON string for class {class_instance.pk}: {options_json_string}"
                )
                options_data_list = json.loads(options_json_string)

                if not (
                    isinstance(options_data_list, list) and len(options_data_list) == 1
                ):
                    logger.error(
                        f"Invalid format for 'options' data for class {class_instance.classId}. Expected a list with one object."
                    )
                    raise DRFValidationError(
                        {
                            "options": "Options data must be a list containing a single option object."
                        }
                    )

                option_dict = options_data_list[0]
                logger.info(f"Processing single option data: {option_dict}")

                option_image_file_check = request.FILES.get("option_0_image")
                if option_image_file_check:
                    logger.warning(
                        f"Received 'option_0_image' file for class {class_instance.pk}, but ClassOption does not have an image field. This file will be ignored."
                    )

                # --- Create Option Instance (without title, description, or image) ---
                try:
                    ClassOption.objects.create(
                        classId=class_instance,
                        booking_type=option_dict.get(
                            "booking_type",
                            ClassOption._meta.get_field("booking_type").get_default(),
                        ),
                        level=option_dict.get(
                            "level", ClassOption._meta.get_field("level").get_default()
                        ),
                        equipment=option_dict.get("equipment", []),
                        tags=option_dict.get("tags", []),
                        cancellationPolicy=option_dict.get(
                            "cancellationPolicy",
                            ClassOption._meta.get_field(
                                "cancellationPolicy"
                            ).get_default(),
                        ),
                        cancellationRefundPercentage=option_dict.get(
                            "cancellationRefundPercentage",
                            ClassOption._meta.get_field(
                                "cancellationRefundPercentage"
                            ).get_default(),
                        ),
                        price_type=option_dict.get(
                            "price_type",
                            ClassOption._meta.get_field("price_type").get_default(),
                        ),
                    )
                    logger.info(
                        f"Created ClassOption for class {class_instance.classId}."
                    )
                except Exception as e:
                    logger.error(
                        f"Error creating ClassOption for class {class_instance.classId}: {str(e)}",
                        exc_info=True,
                    )
                    raise DRFValidationError(
                        {"options": f"Failed to create class option: {str(e)}"}
                    )

            else:
                logger.warning(
                    f"No 'options' JSON string found in request data for class {class_instance.pk}"
                )
                raise DRFValidationError({"options": "Class option data is required."})

        except json.JSONDecodeError:
            logger.error(
                f"Invalid JSON in 'options' field for class {class_instance.classId} creation."
            )
            raise DRFValidationError(
                {"options": "Invalid JSON format for options data."}
            )
        except (
            DRFValidationError
        ):  # Re-raise validation errors to ensure transaction rollback
            raise
        except Exception as e:  # Catch any other unexpected error
            logger.error(
                f"Unexpected error processing images/options for class {class_instance.classId}: {e}",
                exc_info=True,
            )
            raise DRFValidationError(
                f"An unexpected error occurred while processing class creation: {str(e)}"
            )

    def perform_update(self, serializer):
        instance = serializer.instance  # Get instance before saving serializer
        user = self.request.user
        request_data = self.request.data  # Get raw request data

        # --- Handle status changes carefully ---
        new_status = serializer.validated_data.get("status")
        if new_status == "suspended":
            if instance.status != "suspended":
                raise PermissionDenied("You do not have permission to suspend a class.")
            # Allow saving if status is already suspended (no change needed)
            serializer.validated_data.pop("status", None)

        # --- Start Transaction ---
        with transaction.atomic():
            # --- Prepare category/subcategory for update (THE FIX) ---
            update_kwargs = {}
            category_key = request_data.get("category_key")

            # Check if category_key was provided in the request
            if category_key is not None:
                try:
                    new_category = ClassCategory.objects.get(key=category_key)
                    update_kwargs["category"] = new_category
                    logger.info(
                        f"Preparing to update category for class {instance.pk} to '{new_category.name}'."
                    )

                    # When category changes, subcategory MUST be re-evaluated.
                    subcategory_key = request_data.get("subcategory_key")
                    if subcategory_key:
                        new_subcategory = ClassSubcategory.objects.get(
                            category=new_category, key=subcategory_key
                        )
                        update_kwargs["subcategory"] = new_subcategory
                        logger.info(
                            f"Preparing to update subcategory for class {instance.pk} to '{new_subcategory.name}'."
                        )
                    else:
                        # If a new category is set but no subcategory is provided, clear the subcategory.
                        update_kwargs["subcategory"] = None
                        logger.info(
                            f"Clearing subcategory for class {instance.pk} due to category change."
                        )

                except ClassCategory.DoesNotExist:
                    raise DRFValidationError(
                        {
                            "category_key": f"Category with key '{category_key}' not found."
                        }
                    )
                except ClassSubcategory.DoesNotExist:
                    raise DRFValidationError(
                        {
                            "subcategory_key": f"Subcategory with key '{subcategory_key}' not found in the selected category."
                        }
                    )

            # Save the main class instance first with allowed changes from the serializer
            # and the manually prepared category/subcategory changes.
            updated_instance = serializer.save(**update_kwargs)
            logger.info(
                f"Class '{updated_instance.title}' (ID: {updated_instance.pk}) base fields updated by user {user.email}"
            )

            # --- Process Image Updates ---
            request_files = self.request.FILES

            # 1. Delete images marked for deletion
            try:
                delete_ids_json = request_data.get("delete_image_ids", "[]")
                delete_ids = json.loads(delete_ids_json)
                if delete_ids:
                    images_to_delete = ClassImage.objects.filter(
                        classId=instance, imageId__in=delete_ids
                    )
                    for img in images_to_delete:
                        if img.image:
                            try:
                                default_storage.delete(img.image.name)
                            except Exception as e:
                                logger.warning(
                                    f"Could not delete S3 file for image {img.imageId} during update: {e}"
                                )
                    deleted_count, _ = images_to_delete.delete()
                    if deleted_count:
                        logger.info(
                            f"Deleted {deleted_count} images for class {instance.pk} based on request."
                        )
            except json.JSONDecodeError:
                logger.warning(
                    f"Invalid JSON for delete_image_ids for class {instance.pk}"
                )
            except Exception as e:
                logger.error(
                    f"Error deleting images for class {instance.pk}: {e}", exc_info=True
                )

            # 2. Add new images
            new_image_files = request_files.getlist("images")
            newly_created_images = []
            if new_image_files:
                img_objects = [
                    ClassImage(classId=instance, image=f) for f in new_image_files
                ]
                newly_created_images = ClassImage.objects.bulk_create(img_objects)
                logger.info(
                    f"Added {len(newly_created_images)} new images for class {instance.pk}."
                )

            # 3. Set Cover Image
            cover_image_id = request_data.get("cover_image_id")
            cover_image_filename = request_data.get(
                "cover_image_filename"
            )  # For newly uploaded cover

            new_cover_set = False
            if cover_image_filename:
                # Find the newly uploaded image by filename
                found_new_cover = False
                for img_instance in newly_created_images:
                    if img_instance.image.name.endswith(
                        cover_image_filename
                    ):  # Basic check
                        ClassImage.objects.filter(classId=instance).update(
                            isCover=False
                        )
                        img_instance.isCover = True
                        img_instance.save(update_fields=["isCover"])
                        logger.info(
                            f"Set newly uploaded image '{cover_image_filename}' as cover for class {instance.pk}"
                        )
                        new_cover_set = True
                        found_new_cover = True
                        break
                if not found_new_cover:
                    logger.warning(
                        f"Could not find newly uploaded image with filename '{cover_image_filename}' to set as cover for class {instance.pk}"
                    )

            elif cover_image_id and not new_cover_set:
                try:
                    cover_image_id_int = int(cover_image_id)
                    if ClassImage.objects.filter(
                        classId=instance, imageId=cover_image_id_int
                    ).exists():
                        ClassImage.objects.filter(classId=instance).exclude(
                            imageId=cover_image_id_int
                        ).update(isCover=False)
                        ClassImage.objects.filter(
                            classId=instance, imageId=cover_image_id_int
                        ).update(isCover=True)
                        logger.info(
                            f"Set existing image ID {cover_image_id_int} as cover for class {instance.pk}"
                        )
                        new_cover_set = True
                    else:
                        logger.warning(
                            f"Requested cover image ID {cover_image_id} not found or doesn't belong to class {instance.pk}"
                        )
                except (ValueError, TypeError):
                    logger.warning(f"Invalid cover_image_id format: {cover_image_id}")

            if not new_cover_set:
                remaining_images = ClassImage.objects.filter(classId=instance)
                if (
                    remaining_images.exists()
                    and not remaining_images.filter(isCover=True).exists()
                ):
                    first_image = remaining_images.first()
                    first_image.isCover = True
                    first_image.save(update_fields=["isCover"])
                    logger.info(
                        f"Set image ID {first_image.imageId} as default cover for class {instance.pk} as no specific cover was set/found."
                    )

            # --- Process Option Updates (Assuming single option model) ---
            options_json_string = request_data.get("options")
            if options_json_string:
                try:
                    options_data = json.loads(options_json_string)
                    if isinstance(options_data, list) and len(options_data) > 0:
                        option_dict = options_data[
                            0
                        ]  # Get the first (only) option data
                        option_id = option_dict.get("optionId")
                        option_instance = None
                        if option_id:
                            try:
                                option_instance = ClassOption.objects.get(
                                    optionId=option_id, classId=instance
                                )
                            except ClassOption.DoesNotExist:
                                logger.warning(
                                    f"Option ID {option_id} provided but not found for class {instance.pk}. Cannot update."
                                )

                        if option_instance:
                            option_dict.pop("title", None)
                            option_dict.pop("description", None)
                            option_dict.pop("image", None)

                            option_serializer = ManagedClassOptionSerializer(
                                option_instance,
                                data=option_dict,
                                partial=True,
                                context=serializer.context,
                            )
                            if option_serializer.is_valid():
                                # ClassOption no longer has an image field.
                                # All direct image handling for ClassOption is removed.
                                option_instance = option_serializer.save()
                                logger.info(
                                    f"Updated option ID {option_instance.optionId} for class {instance.pk}"
                                )
                            else:
                                logger.error(
                                    f"Option data validation failed for option {option_id}: {option_serializer.errors}"
                                )
                                raise DRFValidationError(
                                    {
                                        "options": f"Validation failed for option {option_id}: {option_serializer.errors}"
                                    }
                                )
                        else:
                            logger.warning(
                                f"Could not find existing option to update for class {instance.pk} based on provided data: {option_dict}. Ensure optionId is correct."
                            )
                            if option_id:
                                raise DRFValidationError(
                                    {
                                        "options": f"Option with ID {option_id} not found for this class."
                                    }
                                )

                except json.JSONDecodeError:
                    logger.error(
                        f"Invalid JSON in 'options' field during update for class {instance.pk}."
                    )
                    raise DRFValidationError(
                        {"options": "Invalid JSON format for options data."}
                    )
                except DRFValidationError:  # Re-raise validation errors from serializer
                    raise
                except Exception as e:
                    logger.error(
                        f"Error processing options update for class {instance.pk}: {e}",
                        exc_info=True,
                    )
                    raise DRFValidationError(
                        f"An error occurred while updating class options: {str(e)}"
                    )

    def perform_destroy(self, instance):
        # Permissions checked by get_object.
        class_title = instance.title
        class_pk = instance.pk
        user_email = self.request.user.email

        # Business users should only deactivate (soft delete)
        try:
            instance.status = "suspended"
            instance.save(update_fields=["status"])
            logger.info(
                f"Class '{class_title}' (ID: {class_pk}) deactivated by business user {user_email}"
            )
        except Exception as e:
            logger.error(
                f"Error deactivating class {class_pk}: {str(e)}", exc_info=True
            )
            raise DRFValidationError(f"Could not deactivate class: {str(e)}")

    # --- Custom Actions for Business Management ---

    @action(detail=False, methods=["get"], url_path="contact-info")
    def contact_info(self, request, *args, **kwargs):
        """
        Retrieves the primary contact information for the user's associated business.
        Used to pre-fill forms like the class creation contact step.
        """
        user = request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            raise NotFound("No active business profile found for this user.")

        serializer = BusinessContactInfoSerializer(business)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def images(self, request, pk=None):
        """Upload new images for a class."""
        class_instance = self.get_object()  # Checks permissions
        images_data = request.FILES.getlist("images")
        if not images_data:
            raise DRFValidationError({"images": "No image files provided."})

        created_images = []
        errors = []
        with transaction.atomic():
            for image_file in images_data:
                serializer = ClassImageSerializer(data={"image": image_file})
                if serializer.is_valid():
                    img = serializer.save(classId=class_instance)
                    created_images.append(img)
                else:
                    errors.append({image_file.name: serializer.errors})

        if errors:
            logger.warning(f"Image upload partially failed for class {pk}: {errors}")
            return Response(
                {
                    "message": "Some images failed.",
                    "uploaded": ClassImageSerializer(created_images, many=True).data,
                    "errors": errors,
                },
                status=status.HTTP_207_MULTI_STATUS,
            )

        logger.info(
            f"{len(created_images)} images added to class '{class_instance.title}' by {request.user.email}"
        )
        return Response(
            ClassImageSerializer(created_images, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["delete"], url_path="images/(?P<image_id>[^/.]+)")
    def delete_image(self, request, pk=None, image_id=None):
        """Delete a specific class image."""
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
            raise NotFound("Image not found for this class.")  # Use NotFound
        except Exception as e:
            logger.error(
                f"Error deleting image {image_id} for class {pk}: {e}", exc_info=True
            )
            raise DRFValidationError({"error": f"Failed to delete image: {e}"})

    @action(
        detail=True, methods=["patch"], url_path="toggle-active"
    )  # More RESTful path
    def toggle_class_active(self, request, pk=None):
        """Toggle class active/inactive status (Business user action)."""
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
        return Response({"status": class_instance.status})


# --- Views for related models (Options, Schedules, Instances, Breaks) ---


class BusinessClassOptionDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ManagedClassOptionSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
        IsVerifiedAndActiveBusinessOwnerOrManager,
    ]
    lookup_url_kwarg = "option_id"
    lookup_field = "optionId"  # Match model field

    def get_queryset(self):
        user = self.request.user
        class_pk = self.kwargs.get("pk")
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
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
        instance.schedules.update(is_active=False)
        instance.delete()
        logger.info(f"ClassOption ID {option_id} deleted by {self.request.user.email}")


class BusinessScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
        IsVerifiedAndActiveBusinessOwnerOrManager,
    ]

    def get_queryset(self):
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return Schedule.objects.none()

        queryset = Schedule.objects.filter(option__classId__businessId=business)

        option_id = self.request.query_params.get("option_id")
        if option_id and option_id.isdigit():
            queryset = queryset.filter(
                option_id=option_id, option__classId__businessId=business
            )

        # Prefetch related instances and their confirmed bookings count for efficiency in serializer
        queryset = (
            queryset.select_related("option", "option__classId")
            .prefetch_related(
                Prefetch(
                    "instances",
                    queryset=ScheduleInstance.objects.all().select_related("schedule"),
                ),  # Prefetch all instances
                Prefetch(
                    "instances__bookings",
                    queryset=Booking.objects.filter(status="confirmed"),
                ),  # Prefetch confirmed bookings for those instances
            )
            .order_by("option__classId__title", "day", "time")
        )
        return queryset

    def perform_create(self, serializer):
        option = serializer.validated_data.get("option")
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business or not option or option.classId.businessId != business:
            raise PermissionDenied(
                "Cannot create schedule for an option not belonging to your business."
            )

        # The serializer now only saves the Schedule, not the instance.
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
                status="scheduled",
            )
            logger.info(
                f"Single Session Schedule and its instance created for Option ID {option.optionId} by {user.email}"
            )

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
                            name=data.get("name"),  # Pass the name to each new schedule
                            option=option,
                            date=current_date,
                            time=time_val,
                            duration=data["duration"],
                            price=data["price"],
                            maxParticipants=data["maxParticipants"],
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
                        status="scheduled",
                    )
                    for schedule in created_schedules
                ]
                ScheduleInstance.objects.bulk_create(instances_to_create)

            logger.info(
                f"Bulk created {len(created_schedules)} schedules for option {option.pk} by {request.user.email} ({skipped_count} skipped)."
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

    def perform_update(self, serializer):
        instance = self.get_object()
        serializer.validated_data.pop("option", None)
        updated_instance = serializer.save()
        logger.info(f"Schedule ID {instance.pk} updated by {self.request.user.email}")

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

        # If no confirmed bookings, proceed with deletion (which cascades to instances)
        instance.delete()
        logger.info(
            f"Schedule ID {schedule_id} deleted by {self.request.user.email} (hard delete executed)."
        )


class BusinessScheduleInstanceViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleInstanceSerializer
    permission_classes = [
        IsAuthenticated,
        CanManageOwnClasses,
        IsVerifiedAndActiveBusinessOwnerOrManager,
    ]
    http_method_names = ["get", "post", "patch", "head", "options"]  # No PUT/DELETE

    def get_queryset(self):
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return ScheduleInstance.objects.none()
        queryset = (
            ScheduleInstance.objects.filter(
                schedule__option__classId__businessId=business
            )
            .select_related("schedule__option__classId")
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
