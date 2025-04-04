from rest_framework import viewsets, status, filters 
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission 
from django.db.models import (Q, Min, Max, Avg, Count, F, Sum, Value, 
                              CharField, FloatField, Subquery, Exists, OuterRef) 
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.http import HttpResponse # For CSV export
import csv # For CSV export

from ...models import (
    ClassCategory, ClassSubcategory, ClassesMain, ClassOption, Schedule, 
    ScheduleInstance, Booking, Reviews
)
from ...serializers.class_management.class_management_serializers import (
    AdminClassSerializer,
    AdminClassDetailSerializer,
    AdminClassCreateSerializer, 
    AdminClassCategorySerializer,
    AdminReviewSerializer,
    SubcategorySerializer
)

import logging
logger = logging.getLogger(__name__)

# --- Custom Permission Classes (Example - Adapt as needed) ---

class CanAccessClassAdmin(BasePermission):
    message = "You do not have permission to access class administration."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        return request.user.has_perm('quickstart.access_class_admin')

class CanAccessCategoryAdmin(BasePermission):
    message = "You do not have permission to access category administration."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        return request.user.has_perm('quickstart.access_category_admin')

class CanAccessReviewAdmin(BasePermission):
    message = "You do not have permission to access review moderation."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active: return False
        return request.user.has_perm('quickstart.access_review_admin')


# --- AdminClassViewSet ---

class AdminClassViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing classes
    """
    # Base permission for the viewset
    permission_classes = [IsAuthenticated, CanAccessClassAdmin]
    # Note: Default ModelViewSet provides list, retrieve, update, partial_update, destroy
    # We will override or implement permissions within each method as needed.

    # Define http methods - primarily read-only + custom actions for admin
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options', 'trace'] # Allow POST/PATCH/DELETE for specific actions/overrides
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'businessId__businessName', 'category__name', 'location'] # Use related field names
    ordering_fields = ['title', 'createdAt', 'average_rating', 'review_count', 'active_schedules_count'] # Define orderable fields
    ordering = ['-createdAt'] # Default ordering

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return AdminClassDetailSerializer
        return AdminClassSerializer 

    def get_queryset(self):
        # Basic permission check for viewing any class
        # Note: This check runs early. Finer checks happen in methods.
        if not self.request.user.has_perm('quickstart.view_classesmain'):
             # Log and return empty queryset if user cannot view any classes
             logger.warning(f"User {self.request.user.email} denied access to list classes (missing view_classesmain perm).")
             return ClassesMain.objects.none()

        try:
            # Use select_related for efficiency
            queryset = ClassesMain.objects.select_related(
                'businessId', 'category', 'subcategory'
            ).distinct() # Use distinct with annotations spanning multiple tables

            # Annotations...
            queryset = queryset.annotate(
                 business_name=F('businessId__businessName'),
                 average_rating=Coalesce(Avg('reviews__rating'), Value(0.0), output_field=FloatField()),
                 review_count=Count('reviews', distinct=True),
                 min_price=Min('options__schedules__price'),
                 max_price=Max('options__schedules__price'),
                 active_schedules_count=Count(
                     'options__schedules__instances',
                     filter=Q(
                         options__schedules__instances__date__gte=timezone.now().date(),
                         options__schedules__instances__status='scheduled'
                     ),
                     distinct=True
                 )
            )

            # --- Filtering Logic ---
            category_id = self.request.query_params.get('category_id') # Use ID
            if category_id and category_id.isdigit():
                queryset = queryset.filter(category_id=category_id)

            status_filter = self.request.query_params.get('status')
            if status_filter: # Validate against choices
                 valid_statuses = [choice[0] for choice in ClassesMain.STATUS_CHOICES]
                 if status_filter in valid_statuses:
                      queryset = queryset.filter(status=status_filter)

            featured = self.request.query_params.get('featured')
            if featured is not None:
                 is_featured = str(featured).lower() in ['true', '1', 'yes']
                 queryset = queryset.filter(businessId__featured=is_featured)

            return queryset

        except Exception as e:
            logger.error(f"Error in AdminClassViewSet.get_queryset: {str(e)}", exc_info=True)
            return ClassesMain.objects.none() # Return empty on error

    # --- Standard Actions Overridden for Permissions ---

    def list(self, request, *args, **kwargs):
         # Permission already checked implicitly by get_queryset returning none() if no perm
         return super().list(request, *args, **kwargs)


    def retrieve(self, request, *args, **kwargs):
        # Check individual object view permission
        if not request.user.has_perm('quickstart.view_classesmain'):
             self.permission_denied(request, message="You cannot view class details.")

        return super().retrieve(request, *args, **kwargs)

    # We are primarily using custom actions, but if you needed standard update/delete:
    def partial_update(self, request, *args, **kwargs):
        """ Example: Allows admin to update basic class details if permitted """
        if not request.user.has_perm('quickstart.change_classesmain'):
            self.permission_denied(request, message="You cannot update class details.")
        instance = self.get_object()
        return super().partial_update(request, *args, **kwargs) # Default behaviour if no specific logic

    def destroy(self, request, *args, **kwargs):
        """ Allows admin to delete a class if permitted """
        if not request.user.has_perm('quickstart.delete_classesmain'):
            self.permission_denied(request, message="You cannot delete classes.")
        instance = self.get_object()
        # Optional hierarchy check
        # if not user_can_manage(request.user, instance.businessId.owner): ...
        logger.warning(f"Class '{instance.title}' (ID: {instance.pk}) deleted by Admin {request.user.email}")
        return super().destroy(request, *args, **kwargs)


    # --- Custom Actions ---

    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Get analytics for classes"""
        if not request.user.has_perm('quickstart.view_class_analytics'):
            self.permission_denied(request, message="You cannot view class analytics.")

        # --- Analytics calculation logic ---
        total_classes = ClassesMain.objects.count()
        active_classes = ClassesMain.objects.filter(status='active').count()
        avg_rating_result = Reviews.objects.aggregate(avg=Coalesce(Avg('rating'), Value(0.0)))
        avg_rating = avg_rating_result['avg']

        # Ensure category breakdown uses category names or IDs consistently
        category_counts = list(ClassesMain.objects.filter(category__isnull=False).values(
             id=F('category__id'), name=F('category__name'), color=F('category__color')
        ).annotate(count=Count('classId')).order_by('-count')) # Order by count

        status_counts = dict(ClassesMain.objects.values_list('status').annotate(count=Count('classId')))

        # Use annotations from the main queryset definition if possible for consistency
        popular_classes = list(self.get_queryset().exclude(bookings_count=0).order_by('-bookings_count')[:5].values(
            'classId', 'title', 'bookings_count'
        ))
        top_rated_classes = list(self.get_queryset().exclude(average_rating=0).order_by('-average_rating')[:5].values(
            'classId', 'title', 'average_rating'
        ))

        return Response({
            'totalClasses': total_classes,
            'activeClasses': active_classes,
            'averageRating': round(avg_rating, 1),
            'categoryCounts': category_counts,
            'featuredClasses': ClassesMain.objects.filter(businessId__featured=True).count(),
            'statusCounts': status_counts,
            'popularClasses': popular_classes,
            'topRatedClasses': top_rated_classes
        })

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export class data"""
        if not request.user.has_perm('quickstart.export_class_data'):
            self.permission_denied(request, message="You cannot export class data.")

        # Apply viewset filters to the export
        queryset = self.filter_queryset(self.get_queryset())

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="classes_export.csv"'
        writer = csv.writer(response)

        headers = [ # Match headers with data below
            'Class ID', 'Title', 'Business Name', 'Category', 'Status',
            'Average Rating', 'Review Count', 'Active Schedules Count',
            'Min Price', 'Max Price', 'Created At'
        ]
        writer.writerow(headers)

        # Fetch necessary fields, including annotated ones
        for class_obj in queryset.values(
            'classId', 'title', 'business_name', 'category__name', 'status',
            'average_rating', 'review_count', 'active_schedules_count',
            'min_price', 'max_price', 'createdAt'
        ):
             writer.writerow([
                 class_obj['classId'],
                 class_obj['title'],
                 class_obj['business_name'],
                 class_obj['category__name'] or 'N/A',
                 class_obj['status'],
                 round(class_obj['average_rating'], 1),
                 class_obj['review_count'],
                 class_obj['active_schedules_count'],
                 class_obj['min_price'],
                 class_obj['max_price'],
                 class_obj['createdAt'].strftime('%Y-%m-%d %H:%M:%S') if class_obj['createdAt'] else ''
             ])
        return response


    @action(detail=True, methods=['patch']) # Use PATCH for partial update
    def update_class_status(self, request, pk=None):
        """Admin action to update class status (including suspension)"""
        if not request.user.has_perm('quickstart.change_class_status'):
            self.permission_denied(request, message="You cannot change class status.")

        class_instance = self.get_object()

        new_status = request.data.get('status')
        valid_statuses = [choice[0] for choice in ClassesMain.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response(
                {'error': f'Invalid status value. Must be one of: {", ".join(valid_statuses)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        old_status = class_instance.status
        if old_status == new_status:
             return Response({ # No change needed
                  'status': class_instance.status,
                  'classId': class_instance.classId,
                  'message': 'Status is already set to the requested value.'
             })

        class_instance.status = new_status
        class_instance.save(update_fields=['status'])

        # Log action
        logger.info(f"Class '{class_instance.title}' (ID: {pk}) status changed from {old_status} to {new_status} by Admin {request.user.email}")
        # Optionally add to AuditLog

        # Return updated status and ID
        return Response({
            'status': class_instance.status,
            'classId': class_instance.classId
        })


class AdminCategoryViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing class categories
    """
    permission_classes = [IsAuthenticated, CanAccessCategoryAdmin] # Base permission
    serializer_class = AdminClassCategorySerializer
    queryset = ClassCategory.objects.prefetch_related('subcategories').order_by('name') # Add ordering
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'] # Allow CRUD
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'key']


    # --- Standard CRUD with Permissions ---
    def create(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.add_classcategory'):
            self.permission_denied(request, message="You cannot create categories.")
        # Add logging
        response = super().create(request, *args, **kwargs)
        if response.status_code == status.HTTP_201_CREATED:
             logger.info(f"Category '{response.data.get('name')}' created by Admin {request.user.email}")
        return response

    def update(self, request, *args, **kwargs): # Handles PUT and PATCH
        if not request.user.has_perm('quickstart.change_classcategory'):
            self.permission_denied(request, message="You cannot update categories.")
        # Add logging
        instance = self.get_object()
        old_name = instance.name
        response = super().update(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
             logger.info(f"Category '{old_name}' (ID: {instance.pk}) updated by Admin {request.user.email}")
        return response

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.delete_classcategory'):
            self.permission_denied(request, message="You cannot delete categories.")

        instance = self.get_object()
        # Prevent deletion if classes are using this category
        if instance.classes.exists():
             return Response({"detail": f"Cannot delete category '{instance.name}' as it is used by {instance.classes.count()} classes."}, status=status.HTTP_400_BAD_REQUEST)
        # Prevent deletion if subcategories exist? Optional safety check.
        if instance.subcategories.exists():
             return Response({"detail": f"Cannot delete category '{instance.name}' as it has subcategories. Delete subcategories first."}, status=status.HTTP_400_BAD_REQUEST)

        logger.warning(f"Category '{instance.name}' (ID: {instance.pk}) deleted by Admin {request.user.email}")
        return super().destroy(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
         if not request.user.has_perm('quickstart.view_classcategory'):
              self.permission_denied(request, message="You cannot view categories.")
         return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
         if not request.user.has_perm('quickstart.view_classcategory'):
              self.permission_denied(request, message="You cannot view category details.")
         return super().retrieve(request, *args, **kwargs)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get statistics for categories"""
        if not request.user.has_perm('quickstart.view_category_stats'):
             self.permission_denied(request, message="You cannot view category statistics.")

        # Use the filtered/ordered queryset from the viewset
        queryset = self.filter_queryset(self.get_queryset())

        # Annotate for stats
        categories = queryset.annotate(
            total_classes=Count('classes', distinct=True),
            active_classes=Count('classes', filter=Q(classes__status='active'), distinct=True),
            average_rating=Coalesce(Avg('classes__reviews__rating'), Value(0.0), output_field=FloatField())
        )

        result = []
        for cat in categories:
             # Efficiently count subcategory classes if needed via subqueries or separate queries
             subcat_counts = {s.id: s.classes.count() for s in cat.subcategories.all()} # Uses prefetched data

             result.append({
                 'id': cat.pk,
                 'name': cat.name,
                 'key': cat.key,
                 'color': cat.color,
                 'activeClasses': cat.active_classes,
                 'totalClasses': cat.total_classes,
                 'average_rating': round(cat.average_rating, 1),
                 'subcategories': [
                     {
                         'id': sub.id,
                         'name': sub.name,
                         'count': subcat_counts.get(sub.id, 0)
                     }
                     for sub in cat.subcategories.all() # Access prefetched data
                 ]
             })
        return Response(result)

    # Use nested viewset or separate viewset for subcategories for full CRUD
    # This action is simplified for just adding
    @action(detail=True, methods=['post'], url_path='subcategories')
    def add_subcategory(self, request, pk=None):
        """Add subcategory to a category"""
        if not request.user.has_perm('quickstart.add_classsubcategory'):
            self.permission_denied(request, message="You cannot add subcategories.")

        category = self.get_object()
        serializer = SubcategorySerializer(data=request.data)
        if serializer.is_valid():
            name = serializer.validated_data.get('name')
            key = serializer.validated_data.get('key', name.lower().replace(' ', '_')) # Auto-generate key if needed

            if ClassSubcategory.objects.filter(category=category, key=key).exists():
                return Response({"key": f"Subcategory with key '{key}' already exists for this category."}, status=status.HTTP_400_BAD_REQUEST)
            if ClassSubcategory.objects.filter(category=category, name=name).exists():
                return Response({"name": f"Subcategory with name '{name}' already exists for this category."}, status=status.HTTP_400_BAD_REQUEST)

            # Ensure validated key is used
            serializer.validated_data['key'] = key
            instance = serializer.save(category=category)
            logger.info(f"Subcategory '{instance.name}' added to Category '{category.name}' by Admin {request.user.email}")
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class AdminReviewViewSet(viewsets.ModelViewSet):
    """
    Admin-specific viewset for managing reviews
    """
    permission_classes = [IsAuthenticated, CanAccessReviewAdmin] # Base permission
    serializer_class = AdminReviewSerializer
    # Base queryset with necessary related data for filtering/display
    queryset = Reviews.objects.select_related(
        'userId', 'userId__role', 'classId', 'classId__businessId', 'businessId'
        ).all()
    # Allow GET, PATCH (for moderation), DELETE
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    # Filter backend setup
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['comment', 'userId__email', 'userId__first_name', 'classId__title', 'businessId__businessName', 'report_reason']
    ordering_fields = ['createdAt', 'rating', 'status', 'reported']
    ordering = ['-createdAt'] # Default order

    def get_queryset(self):
        # Apply base permission check for viewing reviews
        if not self.request.user.has_perm('quickstart.view_reviews'):
             self.permission_denied(self.request, message="You cannot view reviews.")

        queryset = super().get_queryset() # Get base queryset with select_related

        # --- Filtering Logic ---
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)

        reported = self.request.query_params.get('reported')
        if reported is not None:
             is_reported = str(reported).lower() in ['true', '1', 'yes']
             queryset = queryset.filter(reported=is_reported)

        rating_filter = self.request.query_params.get('rating')
        if rating_filter and rating_filter.isdigit():
             queryset = queryset.filter(rating=int(rating_filter))

        # Search is handled by SearchFilter
        # Ordering is handled by OrderingFilter

        return queryset

    # --- Standard Actions Overridden for Permissions ---
    def list(self, request, *args, **kwargs):
         # Permission implicitly checked by get_queryset
         return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
         # Permission implicitly checked by base permission
         return super().retrieve(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
         """ Allows admin to moderate review (status, response, reported status) """
         if not request.user.has_perm('quickstart.change_reviews'):
              self.permission_denied(request, message="You cannot moderate reviews.")

         instance = self.get_object()
         # Define fields modifiable by admin
         allowed_fields = ['status', 'business_response', 'reported', 'report_reason']
         update_data = {k: v for k, v in request.data.items() if k in allowed_fields}

         # Basic validation
         if not update_data:
              return Response({"detail": "No valid fields provided for update."}, status=status.HTTP_400_BAD_REQUEST)

         # Validate status if provided
         if 'status' in update_data:
              valid_statuses = [choice[0] for choice in Reviews._meta.get_field('status').choices]
              if update_data['status'] not in valid_statuses:
                   return Response({"status": f"Invalid status. Choose from: {', '.join(valid_statuses)}"}, status=status.HTTP_400_BAD_REQUEST)

         # Validate reported status if provided
         if 'reported' in update_data and not isinstance(update_data['reported'], bool):
              update_data['reported'] = str(update_data['reported']).lower() in ['true', '1', 'yes']


         serializer = self.get_serializer(instance, data=update_data, partial=True)
         serializer.is_valid(raise_exception=True)
         serializer.save()
         logger.info(f"Review ID {instance.pk} updated by Admin {request.user.email}. Changes: {update_data}")
         # Optionally add to AuditLog

         return Response(serializer.data)


    def destroy(self, request, *args, **kwargs):
         """ Allows admin to delete a review """
         if not request.user.has_perm('quickstart.delete_reviews'):
              self.permission_denied(request, message="You cannot delete reviews.")
         instance = self.get_object()
         logger.warning(f"Review ID {instance.pk} deleted by Admin {request.user.email}") # Log deletion
         # Optionally add to AuditLog
         return super().destroy(request, *args, **kwargs)