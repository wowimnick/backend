from decimal import Decimal
from rest_framework import viewsets, status, filters, generics, permissions
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError, NotFound # Added NotFound
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db import transaction
from django.db.models import Q, Prefetch, Count, Avg, OuterRef, Subquery, IntegerField, F, Sum, Value, DecimalField # Added Sum, Value, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.http import Http404
import logging
import json
from django.utils import timezone

from quickstart.serializers.admin.class_management.class_management_serializers import AdminClassCategorySerializer 

from ...models import (
    BusinessInfo, ClassCategory, ClassSubcategory, ClassesMain, ClassImage,
    ClassOption, Schedule, ScheduleInstance, Booking, Reviews
)

from ...serializers import (
    ManagedClassSerializer, ClassCreateSerializer, ClassImageSerializer,
    ManagedClassOptionSerializer, ScheduleSerializer, ScheduleInstanceSerializer
)

from ...utils.permissions import CanManageOwnClasses, IsVerifiedAndActiveBusinessOwnerOrManager

logger = logging.getLogger(__name__)

# --- Business ViewSet for Managing Classes ---

class PublicCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides a list of class categories and their subcategories.
    Accessible by authenticated users (e.g., business owners creating classes).
    """
    permission_classes = [permissions.IsAuthenticated] # Or AllowAny if categories are fully public
    serializer_class = AdminClassCategorySerializer # Adjust if a different serializer is needed
    queryset = ClassCategory.objects.prefetch_related('subcategories').order_by('name')

    def list(self, request, *args, **kwargs):
        # Standard list action, queryset and serializer handle the rest
        return super().list(request, *args, **kwargs)

class BusinessClassViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Business Users to manage their own ClassesMain.
    Handles CRUD, image management, and status toggling.
    (URL Base: /api/business/classes/)
    """
    permission_classes = [IsAuthenticated, CanManageOwnClasses, IsVerifiedAndActiveBusinessOwnerOrManager]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]

    search_fields = ['title', 'description', 'options__title', 'category__name', 'status']
    ordering_fields = ['title', 'createdAt', 'updatedAt', 'status', 'average_rating', 'review_count']
    ordering = ['-updatedAt']

    # --- Subqueries for Annotations (Business Context) ---
    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk')) # No status filter needed? Or 'approved'?
        .values('classId')
        .annotate(avg_rating=Avg('rating'))
        .values('avg_rating')[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1) # Use DecimalField
    )
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk')) # No status filter needed?
        .values('classId')
        .annotate(count=Count('reviewId'))
        .values('count')[:1],
        output_field=IntegerField()
    )

    def get_serializer_class(self):
        if self.action == 'create':
            return ClassCreateSerializer
        # Add specific serializers for actions if needed (e.g., images)
        # if self.action == 'images': return ClassImageSerializer # Not standard, handled in action
        return ManagedClassSerializer # Default for list, retrieve, update

    def get_queryset(self):
        """Filter queryset to only classes belonging to the user's associated business."""
        user = self.request.user
        # CanManageOwnClasses permission is checked by DRF automatically.
        # Find the business associated with the user (owner or manager).
        # This relies on the user being linked correctly to a BusinessInfo instance.
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()

        if not business:
            # Should not happen if permissions are set correctly, but safeguard.
            logger.warning(f"User {user.email} lacks associated business for BusinessClassViewSet.")
            return ClassesMain.objects.none()

        # Filter classes by the user's business
        return ClassesMain.objects.filter(
            businessId=business
        ).exclude(
            status='suspended' 
        ).select_related(
            'businessId', 'category', 'subcategory'
        ).prefetch_related(
            'images',
            # Prefetch options with schedules for management view
            Prefetch('options', queryset=ClassOption.objects.prefetch_related('schedules').order_by('optionId')),
        ).annotate(
            average_rating=Coalesce(self.AVERAGE_RATING_SUBQUERY, Value(Decimal('0.0'))), # Default to Decimal
            review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
        ).distinct()

    def perform_create(self, serializer):
        """Associate the new class with the user's business and handle images/options."""
        user = self.request.user
        # Re-fetch business in case something changed or for explicit context
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            # This should be caught earlier by permissions, but double-check.
            raise PermissionDenied("You must be associated with a business to create a class.")

        # Extract category/subcategory keys from validated data
        category_key = serializer.validated_data.pop('category_key')
        subcategory_key = serializer.validated_data.pop('subcategory_key', None)

        try:
            category = ClassCategory.objects.get(key=category_key)
            subcategory = None
            if subcategory_key:
                subcategory = ClassSubcategory.objects.get(category=category, key=subcategory_key)

            # Create the class instance using remaining validated data
            # Set status to 'active' or 'inactive' based on business policy/default? Defaulting to active.
            instance = serializer.save(
                businessId=business,
                category=category,
                subcategory=subcategory,
                status='active'
            )
            logger.info(f"Class '{instance.title}' (ID: {instance.classId}) created for business '{business.businessName}' by user {user.email}")

            # --- Handle Images and Options AFTER instance is saved ---
            self._process_images_and_options(instance)

        except ClassCategory.DoesNotExist:
             raise DRFValidationError({'category_key': f"Category '{category_key}' not found."})
        except ClassSubcategory.DoesNotExist:
             raise DRFValidationError({'subcategory_key': f"Subcategory '{subcategory_key}' not found in category '{category_key}'."})
        except Exception as e:
             logger.error(f"Error during perform_create for class by {user.email}: {e}", exc_info=True)
             # Reraise a generic validation error or handle specific exceptions
             raise DRFValidationError("An error occurred during class creation.")


    def _process_images_and_options(self, class_instance):
        """Helper method to process images and options from the request."""
        request = self.request # Access the request object

        try:
             # Handle Images from request.FILES
             images_files = request.FILES.getlist('images')
             if images_files:
                 img_objects = [ClassImage(classId=class_instance, image=f) for f in images_files]
                 ClassImage.objects.bulk_create(img_objects)
                 logger.info(f"Bulk uploaded {len(images_files)} images for class {class_instance.classId}")

             # Handle Options from request.data (assuming JSON string in 'options' field)
             options_json_string = request.data.get('options')
             if options_json_string:
                  logger.info(f"Received options JSON string for class {class_instance.pk}: {options_json_string}") # Add Log
                  options_data = json.loads(options_json_string)
                  if isinstance(options_data, list):
                       options_to_create = []
                       for index, option_dict in enumerate(options_data):
                            logger.info(f"Processing option data at index {index}: {option_dict}") # Add Log
                            # --- Validation Check ---
                            if not option_dict.get('title') or not option_dict.get('booking_type'):
                                 logger.warning(f"Skipping invalid option data (missing title or booking_type) at index {index} for class {class_instance.classId}")
                                 continue # Skip this option if essential data is missing

                            option_image_file = request.FILES.get(f'option_{index}_image') # Check for individual option images
                            logger.info(f"Found option image file for index {index}: {'Yes' if option_image_file else 'No'}") # Add Log

                            # --- Create Option Instance Object ---
                            # Ensure all fields match the ClassOption model definition
                            option_instance_data = {
                                 'classId': class_instance, # Link to the created class
                                 'title': option_dict.get('title'),
                                 'description': option_dict.get('description', ''), # Default to empty string
                                 'booking_type': option_dict.get('booking_type'),
                                 'level': option_dict.get('level', 'all'), # Provide default
                                 'equipment': option_dict.get('equipment', []), # Provide default
                                 'tags': option_dict.get('tags', []), # Provide default
                                 'cancellationPolicy': option_dict.get('cancellationPolicy', 'flexible'), # Provide default
                                 'image': option_image_file,
                                 # 'price_type': option_dict.get('price_type', 'per_session'), # Add if this field exists on model
                                 # active=True # REMOVED as per previous fix
                            }
                            logger.info(f"Prepared data for ClassOption: {option_instance_data}") # Add Log

                            try:
                                # Create the unsaved model instance
                                option_model_instance = ClassOption(**option_instance_data)
                                # Add it to the list for bulk creation
                                options_to_create.append(option_model_instance)
                            except TypeError as te:
                                logger.error(f"TypeError creating ClassOption instance data for index {index}: {te}. Data: {option_instance_data}", exc_info=True)
                                continue # Skip this option if constructor fails

                       # --- Bulk Create ---
                       if options_to_create:
                            created_options = ClassOption.objects.bulk_create(options_to_create)
                            logger.info(f"Bulk created {len(created_options)} options for class {class_instance.classId}")
                       else:
                            logger.warning(f"No valid options found to create for class {class_instance.classId}")

                  else:
                       logger.error(f"Invalid format for 'options' data for class {class_instance.classId}. Expected a list, got {type(options_data)}")
                       # raise DRFValidationError({"options": "Invalid format. Expected a list."}) # Optional: Fail request

             else:
                  logger.warning(f"No 'options' JSON string found in request data for class {class_instance.pk}") # Add Log

        except json.JSONDecodeError:
             logger.error(f"Invalid JSON in 'options' field for class creation.")
             # raise DRFValidationError({"options": "Invalid JSON format."}) # Optional: Fail request
        except Exception as e:
             logger.error(f"Error processing images/options for class {class_instance.classId}: {e}", exc_info=True)
             # Don't necessarily fail, but log. Maybe add warning to response later.

    def perform_update(self, serializer):
        instance = serializer.instance # Get instance before saving serializer
        user = self.request.user

        # Prevent changing businessId, category, subcategory via PATCH/PUT
        serializer.validated_data.pop('businessId', None)
        serializer.validated_data.pop('category', None)
        serializer.validated_data.pop('subcategory', None)
        serializer.validated_data.pop('category_key', None)
        serializer.validated_data.pop('subcategory_key', None)

        # Handle status changes carefully
        new_status = serializer.validated_data.get('status')
        if new_status == 'suspended':
            if instance.status != 'suspended':
                raise PermissionDenied("You do not have permission to suspend a class.")
            # Allow saving if status is already suspended (no change needed)
            serializer.validated_data.pop('status', None)


        # --- Start Transaction ---
        with transaction.atomic():
            # Save the main class instance first with allowed changes
            updated_instance = serializer.save()
            logger.info(f"Class '{updated_instance.title}' (ID: {updated_instance.pk}) base fields updated by user {user.email}")

            # --- Process Image Updates ---
            request_data = self.request.data
            request_files = self.request.FILES

            # 1. Delete images marked for deletion
            try:
                delete_ids_json = request_data.get('delete_image_ids', '[]')
                delete_ids = json.loads(delete_ids_json)
                if delete_ids:
                    images_to_delete = ClassImage.objects.filter(classId=instance, imageId__in=delete_ids)
                    for img in images_to_delete:
                        if img.image:
                            try:
                                default_storage.delete(img.image.name)
                            except Exception as e:
                                logger.warning(f"Could not delete S3 file for image {img.imageId} during update: {e}")
                    deleted_count, _ = images_to_delete.delete()
                    if deleted_count:
                        logger.info(f"Deleted {deleted_count} images for class {instance.pk} based on request.")
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON for delete_image_ids for class {instance.pk}")
            except Exception as e:
                logger.error(f"Error deleting images for class {instance.pk}: {e}", exc_info=True)
                # Decide if this should raise an error or just log

            # 2. Add new images
            new_image_files = request_files.getlist('images')
            newly_created_images = []
            if new_image_files:
                img_objects = [ClassImage(classId=instance, image=f) for f in new_image_files]
                newly_created_images = ClassImage.objects.bulk_create(img_objects)
                logger.info(f"Added {len(newly_created_images)} new images for class {instance.pk}.")

            # 3. Set Cover Image
            cover_image_id = request_data.get('cover_image_id')
            cover_image_filename = request_data.get('cover_image_filename') # For newly uploaded cover

            new_cover_set = False
            if cover_image_filename:
                # Find the newly uploaded image by filename
                found_new_cover = False
                for img_instance in newly_created_images:
                    # Note: Comparing request filename to saved filename might be fragile.
                    # Relying on the frontend sending the ID of the *intended* cover might be better,
                    # even if it's a new image (frontend can generate a temp ID).
                    # Assuming filename match is sufficient for now:
                    if img_instance.image.name.endswith(cover_image_filename): # Basic check
                        # Mark all others as not cover first
                        ClassImage.objects.filter(classId=instance).update(isCover=False)
                        img_instance.isCover = True
                        img_instance.save(update_fields=['isCover'])
                        logger.info(f"Set newly uploaded image '{cover_image_filename}' as cover for class {instance.pk}")
                        new_cover_set = True
                        found_new_cover = True
                        break
                if not found_new_cover:
                    logger.warning(f"Could not find newly uploaded image with filename '{cover_image_filename}' to set as cover for class {instance.pk}")

            elif cover_image_id and not new_cover_set:
                try:
                    cover_image_id_int = int(cover_image_id)
                    # Ensure the cover image exists and belongs to this class
                    if ClassImage.objects.filter(classId=instance, imageId=cover_image_id_int).exists():
                        # Mark all others as not cover first
                        ClassImage.objects.filter(classId=instance).exclude(imageId=cover_image_id_int).update(isCover=False)
                        # Mark the selected one as cover
                        ClassImage.objects.filter(classId=instance, imageId=cover_image_id_int).update(isCover=True)
                        logger.info(f"Set existing image ID {cover_image_id_int} as cover for class {instance.pk}")
                        new_cover_set = True
                    else:
                        logger.warning(f"Requested cover image ID {cover_image_id} not found or doesn't belong to class {instance.pk}")
                except (ValueError, TypeError):
                    logger.warning(f"Invalid cover_image_id format: {cover_image_id}")

            # Ensure *a* cover exists if there are any images left
            if not new_cover_set:
                remaining_images = ClassImage.objects.filter(classId=instance)
                if remaining_images.exists() and not remaining_images.filter(isCover=True).exists():
                    first_image = remaining_images.first()
                    first_image.isCover = True
                    first_image.save(update_fields=['isCover'])
                    logger.info(f"Set image ID {first_image.imageId} as default cover for class {instance.pk} as no specific cover was set/found.")


            # --- Process Option Updates (Assuming single option model for now) ---
            options_json_string = request_data.get('options')
            if options_json_string:
                try:
                    options_data = json.loads(options_json_string)
                    if isinstance(options_data, list) and len(options_data) > 0:
                        option_dict = options_data[0] # Get the first (only) option data
                        option_id = option_dict.get('optionId')
                        option_instance = None
                        if option_id:
                            try:
                                option_instance = ClassOption.objects.get(optionId=option_id, classId=instance)
                            except ClassOption.DoesNotExist:
                                logger.warning(f"Option ID {option_id} provided but not found for class {instance.pk}. Cannot update.")
                                # Decide: Create new? Raise error? Log and skip? Skipping for now.

                        if option_instance:
                            # Update existing option
                            option_serializer = ManagedClassOptionSerializer(
                                option_instance,
                                data=option_dict,
                                partial=True, # Allow partial updates
                                context=serializer.context # Pass context if needed
                            )
                            if option_serializer.is_valid():
                                # Handle option image separately
                                option_image_file = request_files.get('option_0_image')
                                if option_image_file:
                                    # Delete old image file if it exists
                                    if option_instance.image:
                                        default_storage.delete(option_instance.image.name)
                                    option_instance.image = option_image_file
                                elif 'option_0_image' in request_data and not request_data.get('option_0_image'):
                                    # Signal to remove image (e.g., empty string sent) - Check frontend signal
                                    # For now, let's assume sending `null` or empty in the form signifies removal
                                    # Or maybe frontend sends a specific flag `remove_option_image: true`
                                    # Let's refine based on frontend signal. Assuming frontend handles this via nulling the image field.
                                    # *If* frontend sends `null` for the image field when it wants removal:
                                    if option_dict.get('image') is None and option_instance.image: # Check if frontend sent null for image
                                        logger.info(f"Removing existing image for option {option_id}")
                                        default_storage.delete(option_instance.image.name)
                                        option_instance.image = None

                                # Save other option fields from serializer
                                option_instance = option_serializer.save(image=option_instance.image) # Ensure image is passed correctly
                                logger.info(f"Updated option ID {option_instance.optionId} for class {instance.pk}")

                            else:
                                logger.error(f"Option data validation failed for option {option_id}: {option_serializer.errors}")
                                # Raise error or log? Raising might be better.
                                raise DRFValidationError({'options': f"Validation failed for option {option_id}: {option_serializer.errors}"})

                        else:
                            logger.warning(f"Could not find existing option to update for class {instance.pk} based on provided data: {option_dict}")
                            # Handle case where option should be created if none exists? Unlikely in update scenario.

                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON in 'options' field during update for class {instance.pk}.")
                    raise DRFValidationError({"options": "Invalid JSON format."})
                except Exception as e:
                    logger.error(f"Error processing options update for class {instance.pk}: {e}", exc_info=True)
                    raise DRFValidationError("An error occurred while updating class options.")
                
    def perform_destroy(self, instance):
        # Permissions checked by get_object.
        class_title = instance.title
        class_pk = instance.pk
        user_email = self.request.user.email

        # Business users should only deactivate (soft delete)
        try:
            instance.status = 'suspended'
            instance.save(update_fields=['status'])
            # Optionally deactivate related options/schedules here if needed
            # instance.options.update(active=False)
            # Schedule.objects.filter(option__classId=instance).update(is_active=False)
            # Consider cancelling future bookings associated with this class's instances
            logger.info(f"Class '{class_title}' (ID: {class_pk}) deactivated by business user {user_email}")
        except Exception as e:
            logger.error(f"Error deactivating class {class_pk}: {str(e)}", exc_info=True)
            raise DRFValidationError(f"Could not deactivate class: {str(e)}")


    # --- Custom Actions for Business Management ---

    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def images(self, request, pk=None):
        """Upload new images for a class."""
        class_instance = self.get_object() # Checks permissions
        images_data = request.FILES.getlist('images')
        if not images_data:
             raise DRFValidationError({"images": "No image files provided."})

        created_images = []
        errors = []
        with transaction.atomic():
            for image_file in images_data:
                serializer = ClassImageSerializer(data={'image': image_file})
                if serializer.is_valid():
                    img = serializer.save(classId=class_instance)
                    created_images.append(img)
                else:
                    errors.append({image_file.name: serializer.errors})

        if errors:
             logger.warning(f"Image upload partially failed for class {pk}: {errors}")
             return Response({
                 "message": "Some images failed.",
                 "uploaded": ClassImageSerializer(created_images, many=True).data,
                 "errors": errors
             }, status=status.HTTP_207_MULTI_STATUS)

        logger.info(f"{len(created_images)} images added to class '{class_instance.title}' by {request.user.email}")
        return Response(ClassImageSerializer(created_images, many=True).data, status=status.HTTP_201_CREATED)


    @action(detail=True, methods=['delete'], url_path='images/(?P<image_id>[^/.]+)')
    def delete_image(self, request, pk=None, image_id=None):
        """Delete a specific class image."""
        class_instance = self.get_object()
        try:
            image = get_object_or_404(ClassImage, imageId=image_id, classId=class_instance)
            if image.image:
                try:
                    image.image.delete(save=False)
                except Exception as e:
                    logger.warning(f"Could not delete S3 file for image {image_id}: {e}")

            image_id_log = image.imageId
            image.delete()
            logger.info(f"Image ID {image_id_log} deleted from class '{class_instance.title}' by {request.user.email}")
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Http404:
             raise NotFound('Image not found for this class.') # Use NotFound
        except Exception as e:
             logger.error(f"Error deleting image {image_id} for class {pk}: {e}", exc_info=True)
             raise DRFValidationError({'error': f"Failed to delete image: {e}"})


    @action(detail=True, methods=['patch'], url_path='toggle-active') # More RESTful path
    def toggle_class_active(self, request, pk=None):
        """Toggle class active/inactive status (Business user action)."""
        class_instance = self.get_object()
        if class_instance.status == 'suspended':
             raise PermissionDenied("Suspended classes cannot be toggled. Contact support.")

        new_status = 'inactive' if class_instance.status == 'active' else 'active'
        old_status = class_instance.status

        class_instance.status = new_status
        class_instance.save(update_fields=['status'])
        logger.info(f"Class '{class_instance.title}' status toggled from {old_status} to {new_status} by {request.user.email}")
        return Response({'status': class_instance.status})


# --- Views for related models (Options, Schedules, Instances, Breaks) ---

class BusinessClassOptionDetail(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ManagedClassOptionSerializer
    permission_classes = [IsAuthenticated, CanManageOwnClasses]
    lookup_url_kwarg = 'option_id'
    lookup_field = 'optionId' # Match model field

    def get_queryset(self):
        user = self.request.user
        class_pk = self.kwargs.get('pk')
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business: return ClassOption.objects.none()
        # Ensure the class_pk belongs to the business as well
        return ClassOption.objects.filter(classId_id=class_pk, classId__businessId=business)

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
        serializer.validated_data.pop('classId', None) # Don't change parent class
        instance = serializer.save()
        logger.info(f"ClassOption ID {instance.optionId} updated by {self.request.user.email}")

    def perform_destroy(self, instance):
        option_id = instance.optionId
        if instance.image: instance.image.delete(save=False)
        # Deactivate related schedules before deleting option
        instance.schedules.update(is_active=False)
        instance.delete()
        logger.info(f"ClassOption ID {option_id} deleted by {self.request.user.email}")


class BusinessScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleSerializer
    permission_classes = [IsAuthenticated, CanManageOwnClasses]

    def get_queryset(self):
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business: return Schedule.objects.none()
        queryset = Schedule.objects.filter(option__classId__businessId=business)
        option_id = self.request.query_params.get('option_id')
        if option_id and option_id.isdigit():
            # Further ensure the requested option_id also belongs to the business
            queryset = queryset.filter(option_id=option_id, option__classId__businessId=business)
        return queryset.select_related('option').order_by('option__title', 'day', 'time') # Improved ordering

    def perform_create(self, serializer):
        option = serializer.validated_data.get('option')
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        # Check both business existence and option ownership
        if not business or not option or option.classId.businessId != business:
             raise PermissionDenied("Cannot create schedule for an option not belonging to your business.")
        instance = serializer.save() # Model save handles instances
        logger.info(f"Schedule created for Option ID {option.optionId} by {user.email}")

    def perform_update(self, serializer):
         instance = self.get_object() # Ensures correct schedule via get_queryset
         serializer.validated_data.pop('option', None) # Don't change parent option
         updated_instance = serializer.save() # Model's save should handle instance updates/regen
         logger.info(f"Schedule ID {instance.pk} updated by {self.request.user.email}")

    def perform_destroy(self, instance):
        # get_object ensures correct schedule
        schedule_id = instance.pk
        # Use model's soft delete
        instance.delete(force_delete=False)
        logger.info(f"Schedule ID {schedule_id} deactivated by {self.request.user.email}")

class BusinessScheduleInstanceViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduleInstanceSerializer
    permission_classes = [IsAuthenticated, CanManageOwnClasses]
    http_method_names = ['get', 'post', 'patch', 'head', 'options'] # No PUT/DELETE

    def get_queryset(self):
        user = self.request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business: return ScheduleInstance.objects.none()
        queryset = ScheduleInstance.objects.filter(
            schedule__option__classId__businessId=business
        ).select_related(
            'schedule__option__classId'
        ).annotate(
            current_bookings_count=Coalesce(
                 Subquery(
                      Booking.objects.filter(schedule_instance=OuterRef('pk'), status='confirmed')
                      .values('schedule_instance').annotate(total_pax=Sum('participants')).values('total_pax')[:1]
                 ), 0, output_field=IntegerField()
            )
        )
        # --- Apply Filters ---
        schedule_id = self.request.query_params.get('schedule_id')
        option_id = self.request.query_params.get('option_id')
        class_id = self.request.query_params.get('class_id')
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        status_filter = self.request.query_params.get('status')

        if schedule_id and schedule_id.isdigit(): queryset = queryset.filter(schedule_id=schedule_id)
        if option_id and option_id.isdigit(): queryset = queryset.filter(schedule__option_id=option_id)
        if class_id and class_id.isdigit(): queryset = queryset.filter(schedule__option__classId_id=class_id)
        if start_date: queryset = queryset.filter(date__gte=start_date)
        if end_date: queryset = queryset.filter(date__lte=end_date)
        if status_filter: queryset = queryset.filter(status=status_filter)

        return queryset.order_by('date', 'time')

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        instance = self.get_object()
        reason = request.data.get('reason', '').strip()
        if not reason: raise DRFValidationError({'reason': 'Reason required.'})
        if instance.status != 'scheduled': raise DRFValidationError({'status': 'Only scheduled can be cancelled.'})
        # Add past date check?
        # if instance.date < timezone.now().date(): raise DRFValidationError({'date': 'Cannot cancel past instances.'})

        with transaction.atomic():
            instance.status = 'cancelled'; instance.cancellation_reason = reason
            instance.save(update_fields=['status', 'cancellation_reason'])
            cancelled_count = Booking.objects.filter(schedule_instance=instance, status='confirmed').update(
                 status='cancelled', cancelled_at=timezone.now(), cancellation_reason=f"Session cancelled: {reason}"
            )
            logger.info(f"Cancelled {cancelled_count} bookings for instance {pk} by {request.user.email}")
        # TODO: Notifications
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=['post'], url_path='mark-attendance')
    def mark_attendance(self, request, pk=None):
         instance = self.get_object()
         if instance.status != 'scheduled': raise DRFValidationError({"detail": "Can only mark attendance for scheduled sessions."})
         # Add time check - allow marking shortly before/after?
         # instance_datetime = timezone.make_aware(datetime.combine(instance.date, instance.time))
         # if instance_datetime > timezone.now() + timedelta(minutes=instance.duration + 15): # Allow marking up to 15 min after end
         #     raise DRFValidationError({"detail": "Cannot mark attendance far in the future."})

         bookings_data = request.data.get('bookings', [])
         if not isinstance(bookings_data, list): raise DRFValidationError({"bookings": "Expected list"})

         updated_count = 0; skipped_count = 0; errors = {}
         with transaction.atomic():
             booking_ids = [item.get('booking_id') for item in bookings_data if item.get('booking_id')]
             bookings_dict = {b.id: b for b in Booking.objects.filter(
                  id__in=booking_ids, schedule_instance=instance, status='confirmed'
             ).select_for_update()}

             for item in bookings_data:
                  booking_id = item.get('booking_id'); attended_status = item.get('attended')
                  if booking_id is None or attended_status is None or not isinstance(attended_status, bool):
                      errors[f"item_{bookings_data.index(item)}"] = "Missing/invalid data"; continue
                  booking = bookings_dict.get(booking_id)
                  if not booking: errors[booking_id] = "Booking not found/confirmed"; skipped_count += 1; continue

                  booking.attendance_marked = True; booking.attended = attended_status
                  # Optionally set booking status to completed if attended
                  # if attended_status: booking.status = 'completed'
                  booking.save(update_fields=['attendance_marked', 'attended']) # Add 'status' if changing
                  updated_count += 1

             instance.attendance_marked = True
             instance.instructor_notes = request.data.get('instructor_notes', instance.instructor_notes)
             # Optionally set instance status to completed
             # instance.status = 'completed'
             instance.save(update_fields=['attendance_marked', 'instructor_notes']) # Add 'status' if changing

         logger.info(f"Attendance marked for instance {pk} by {request.user.email}. Updated: {updated_count}")
         if errors:
              return Response({"message": "Partial success with errors.", "updated": updated_count, "skipped": skipped_count, "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
         return Response({"message": "Attendance marked successfully.", "updated_count": updated_count})