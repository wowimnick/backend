# quickstart/views/business_management/business_admin_views.py

from django.db.models import (Count, Sum, Avg, Case, When, F, DecimalField,
                              Q, Value, CharField, Exists, OuterRef, Subquery) # Added Subquery
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek, TruncQuarter, TruncYear
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework import viewsets, status, filters # Added filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission # Import BasePermission
import csv
from django.http import HttpResponse
from io import StringIO
import logging # Added logging

from ...models import BusinessInfo, ClassesMain, ClassOption, Reviews, Booking, CustomUser # Added CustomUser
from ...serializers import (
    BusinessInfoSerializer, # Assuming this is the main serializer for list/retrieve/update
    # BusinessStatsSerializer, # Not used directly in ViewSet methods shown
)
# from ...utils.permissions import check_user_role, IsAdminUser # Ensure these are removed or commented out
# Import the helper function from user admin views (adjust path if necessary)
try:
    from ..user_management.user_admin_views import user_can_manage
except ImportError:
    # Provide a fallback or raise a configuration error if the import fails
    # Fallback (less secure, ignores hierarchy):
    # def user_can_manage(requesting_user, target_user): return True
    # Or raise error:
    raise ImportError("Could not import user_can_manage helper function. Check path.")


logger = logging.getLogger(__name__) # Initialize logger

# --- Custom Permission Classes ---

class CanAccessBusinessAdmin(BasePermission):
    """Allows access only to users with 'access_business_admin' permission."""
    message = "You do not have permission to access business administration."
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
             return False
        return request.user.has_perm('quickstart.access_business_admin')

class CanManageTargetBusiness(BasePermission):
    """Checks if the requesting user can manage the target business based on owner hierarchy."""
    message = "You cannot manage this business due to hierarchy or ownership restrictions."
    def has_object_permission(self, request, view, obj):
        # obj is the BusinessInfo instance
        if not request.user or not request.user.is_authenticated or not request.user.is_active:
             return False
        # Allow if user has a specific override permission (Optional - uncomment if using)
        # if request.user.has_perm('quickstart.manage_all_businesses'):
        #     return True
        # Check if requester can manage the business owner via hierarchy
        if not hasattr(obj, 'owner') or not obj.owner:
             logger.warning(f"BusinessInfo object (ID: {obj.pk}) is missing an owner. Denying management access.")
             return False # Cannot manage if owner is missing
        return user_can_manage(request.user, obj.owner)

# --- ViewSet ---

class BusinessAdminViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing Businesses (Uses Django Permissions & Hierarchy)
    """
    # Base permission
    permission_classes = [IsAuthenticated, CanAccessBusinessAdmin]
    serializer_class = BusinessInfoSerializer
    # Add filters
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    # Expanded search fields
    search_fields = [
        'businessName', 'businessCity', 'businessState', 'businessType',
        'owner__email', 'owner__first_name', 'owner__last_name'
    ]
    # Define ordering fields based on annotations and model fields
    ordering_fields = [
        'businessName', 'createdAt', 'featured', 'rating',
        'classes_count', 'bookings_count', 'revenue', 'calculated_status'
    ]
    ordering = ['-createdAt'] # Default ordering

    def get_queryset(self):
        """Return queryset with annotations for admin views"""
        # Use select_related for owner to optimize fetching owner details
        queryset = BusinessInfo.objects.select_related('owner', 'owner__role').all()

        # Annotate necessary fields first before filtering on them
        queryset = queryset.annotate(
            # Use Subquery/Exists for checking active schedules
            has_active_schedules=Exists(
                ClassOption.objects.filter(
                    classId__businessId=OuterRef('pk'), # Use pk for OuterRef
                    schedules__is_active=True,
                    schedules__instances__status='scheduled',
                    schedules__instances__date__gte=timezone.now().date()
                )
            ),
            # Annotation for counts and revenue
            classes_count=Count('classesmain', distinct=True),
            bookings_count=Count(
                'classesmain__options__schedules__instances__bookings',
                filter=Q(classesmain__options__schedules__instances__bookings__status__in=['confirmed', 'completed']),
                distinct=True
            ),
            revenue=Coalesce(
                Sum(
                    'classesmain__options__schedules__instances__bookings__amount_paid',
                    filter=Q(classesmain__options__schedules__instances__bookings__status__in=['confirmed', 'completed'])
                ),
                Value(0), # Use Value(0) for Coalesce
                output_field=DecimalField(max_digits=10, decimal_places=2)
            ),
            # Fetch rating - ensure 'reviews' related_name exists on BusinessInfo or adjust
            # If Reviews links directly to BusinessInfo:
            rating=Coalesce(Avg('reviews__rating'), Value(0.0), output_field=DecimalField()),
            # If Reviews links via ClassesMain (original approach):
            # rating=Coalesce(Avg('classesmain__reviews__rating'), Value(0.0), output_field=DecimalField()),

            # Calculate status based on annotations and model fields
            calculated_status=Case(
                When(Q(isActive=True) & Q(has_active_schedules=True), then=Value('active')),
                When(Q(isActive=True) & Q(has_active_schedules=False), then=Value('no_schedules')),
                When(verificationStatus='pending', then=Value('pending')),
                default=Value('inactive'),
                output_field=CharField(max_length=20)
            )
        )

        # Apply filters (use self.filter_queryset provided by DRF)
        # Search filtering is handled by SearchFilter
        # Ordering is handled by OrderingFilter

        # Manual filtering for category and status (if not handled by a dedicated filter backend)
        category = self.request.query_params.get('category', None)
        status_param = self.request.query_params.get('status', None)
        featured = self.request.query_params.get('featured', None)

        if category:
            queryset = queryset.filter(classCategory=category)

        if status_param:
            queryset = queryset.filter(calculated_status=status_param)

        if featured is not None:
            is_featured = str(featured).lower() in ['true', '1', 'yes']
            queryset = queryset.filter(featured=is_featured)

        return queryset

    # --- Standard Actions (Override with Permissions/Hierarchy) ---

    def list(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.view_businessinfo'):
            self.permission_denied(request, message="You do not have permission to view businesses.")
        # Apply filtering and pagination from DRF's generic view
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.view_businessinfo'):
            self.permission_denied(request, message="You do not have permission to view business details.")

        instance = self.get_object()
        # Optional hierarchy check for viewing sensitive details
        # if not user_can_manage(request.user, instance.owner):
        #     self.permission_denied(request, message="Hierarchy restriction: Cannot view details for this business owner.")

        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', True) # Default to partial for PATCH
        if not request.user.has_perm('quickstart.change_businessinfo'):
            self.permission_denied(request, message="You do not have permission to update businesses.")

        instance = self.get_object()
        # Apply hierarchy check
        if not user_can_manage(request.user, instance.owner):
             self.permission_denied(request, message="You cannot manage this business due to hierarchy restrictions.")

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # Log action (optional)
        logger.info(f"Business '{instance.businessName}' (ID: {instance.pk}) updated by Admin {request.user.email}")
        # Optionally use AuditLog model here

        if getattr(instance, '_prefetched_objects_cache', None):
            # If 'prefetch_related' has been used, contents of the cache are possibly stale.
            # Ensure the instance is reloaded before returning to the client, following expiry.
            instance._prefetched_objects_cache = {}

        return Response(serializer.data)

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm('quickstart.delete_businessinfo'):
            self.permission_denied(request, message="You do not have permission to delete businesses.")

        instance = self.get_object()
        business_name = instance.businessName # Get name before deletion
        # Apply hierarchy check
        if not user_can_manage(request.user, instance.owner):
            self.permission_denied(request, message="You cannot delete this business due to hierarchy restrictions.")

        # Log action (optional)
        logger.info(f"Business '{business_name}' (ID: {instance.pk}) deleted by Admin {request.user.email}")
        # Optionally use AuditLog model here

        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # --- Custom Actions ---

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessBusinessAdmin, CanManageTargetBusiness])
    def toggle_feature(self, request, pk=None):
        """Toggle featured status for a business (Hierarchy checked by decorator)"""
        # Check specific permission for the action
        if not request.user.has_perm('quickstart.toggle_business_feature'):
             self.permission_denied(request, message="You do not have permission to feature/unfeature businesses.")

        business = self.get_object()
        featured = request.data.get('featured', not business.featured) # Get value from request or toggle

        # Validate input type
        if not isinstance(featured, bool):
             try:
                 # Attempt to convert common string representations
                 featured = str(featured).lower() in ['true', '1', 'yes']
             except Exception:
                 return Response({'detail': 'Invalid value for featured status (must be true or false).'}, status=status.HTTP_400_BAD_REQUEST)


        business.featured = featured
        business.save(update_fields=['featured']) # Optimize save

        # Log action
        action_text = "featured" if featured else "unfeatured"
        logger.info(f"Business '{business.businessName}' (ID: {business.pk}) {action_text} by Admin {request.user.email}")
        # Optionally add to AuditLog as well

        return Response({
            'businessId': business.businessId,
            'featured': business.featured
        })

    @action(detail=False, methods=['get'])
    def metrics(self, request):
        """Get admin dashboard metrics using dynamic category colors"""
        if not request.user.has_perm('quickstart.view_business_metrics'):
            self.permission_denied(request, message="You do not have permission to view business metrics.")

        today = timezone.now().date()
        thirty_days_ago = today - timedelta(days=30)

        # Aggregate basic counts
        business_counts = BusinessInfo.objects.aggregate(
            total_businesses=Count('pk'),
            active_businesses=Count('pk', filter=Q(isActive=True)),
            featured_businesses=Count('pk', filter=Q(featured=True)),
            new_businesses_30d=Count('pk', filter=Q(createdAt__date__gte=thirty_days_ago))
        )

        # Calculate growth rate (can be refined for efficiency)
        businesses_30d_ago_count = BusinessInfo.objects.filter(createdAt__date__lt=thirty_days_ago).count()
        total_business_growth = 0
        if businesses_30d_ago_count > 0:
            total_business_growth = ((business_counts['total_businesses'] - businesses_30d_ago_count) / businesses_30d_ago_count) * 100

        # Total revenue
        total_revenue = Booking.objects.filter(
            status__in=['confirmed', 'completed']
        ).aggregate(
            total=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField(max_digits=12, decimal_places=2))
        )['total']

        # 1. Get all ClassCategory objects with their colors
        from ...models import ClassCategory # Make sure ClassCategory is imported
        all_categories = ClassCategory.objects.all()
        category_color_dict = {category.key: category.color for category in all_categories}
        category_name_dict = {category.key: category.name for category in all_categories} # For display name

        # 2. Group businesses by category key (assuming BusinessInfo.classCategory stores the key)
        category_distribution_qs = BusinessInfo.objects.values(
            'classCategory' # This should be the key like 'academic', 'music'
        ).annotate(
            count=Count('pk')
        ).order_by('-count')

        # 3. Build the distribution list using dynamic names and colors
        category_distribution = []
        for item in category_distribution_qs:
            category_key = item['classCategory']
            if category_key: # Ensure category key exists
                 category_distribution.append({
                    'name': category_name_dict.get(category_key, category_key.capitalize()), # Use stored name or fallback
                    'value': item['count'],
                    'color': category_color_dict.get(category_key, '#64748b') # Use stored color or default
                })

        # Growth trend (Monthly example) - Use helper
        monthly_growth_data = self._get_growth_data('month', 6)

        # Location distribution (Top 12 example)
        location_distribution_qs = BusinessInfo.objects.values(
            'businessCity', 'businessState'
        ).annotate(count=Count('pk')).order_by('-count')[:12]
        location_distribution = [
            {
                'city': loc['businessCity'],
                'state': loc['businessState'],
                'count': loc['count'],
                'region': self.get_region_for_province(loc['businessState'])
            } for loc in location_distribution_qs
        ]

        # Top businesses (reuse get_queryset for consistency with filters/annotations)
        # Ensure get_queryset includes necessary annotations like 'revenue'
        top_businesses_queryset = self.filter_queryset(self.get_queryset()).order_by('-revenue')[:5]
        top_businesses_data = self.get_serializer(top_businesses_queryset, many=True).data


        return Response({
            'total_businesses': business_counts['total_businesses'],
            'active_businesses': business_counts['active_businesses'],
            'total_business_growth': round(total_business_growth, 2),
            'featured_businesses': business_counts['featured_businesses'],
            'new_businesses_30d': business_counts['new_businesses_30d'],
            'total_revenue': float(total_revenue),
            'category_distribution': category_distribution, # Use corrected list
            'growth_trend': monthly_growth_data,
            'location_distribution': location_distribution,
            'top_businesses': top_businesses_data
        })


    def _get_growth_data(self, timeframe='month', periods=6):
        """Helper to calculate growth data for metrics and growth endpoint"""
        today = timezone.now().date()
        result = []
        trunc_func = TruncMonth

        if timeframe == 'week':
             periods = 12; period_length = 7; trunc_func = TruncWeek
        elif timeframe == 'month':
             periods = 6; period_length = 30; trunc_func = TruncMonth
        elif timeframe == 'quarter':
             periods = 4; period_length = 90; trunc_func = TruncQuarter # Approx
        else: # year
             periods = 3; period_length = 365; trunc_func = TruncYear

        start_date_limit = today - timedelta(days=periods * period_length + period_length) # Go back far enough

        # Aggregate businesses by period
        business_growth = BusinessInfo.objects.filter(
            createdAt__date__gte=start_date_limit
        ).annotate(period=trunc_func('createdAt')).values('period').annotate(count=Count('pk')).order_by('period')

        # Aggregate revenue by period
        revenue_growth = Booking.objects.filter(
             status__in=['confirmed', 'completed'],
             booking_date__date__gte=start_date_limit
        ).annotate(period=trunc_func('booking_date')).values('period').annotate(
             total_revenue=Coalesce(Sum('amount_paid'), Value(0), output_field=DecimalField())
        ).order_by('period')

        # Combine data (simple join on period, assumes periods align)
        revenue_dict = {item['period'].strftime('%Y-%m-%d'): float(item['total_revenue']) for item in revenue_growth if item['period']}
        business_dict = {item['period'].strftime('%Y-%m-%d'): item['count'] for item in business_growth if item['period']}
        all_periods = sorted(list(set(revenue_dict.keys()) | set(business_dict.keys())))

        formatted_result = []
        for period_str in all_periods[-periods:]: # Take the most recent 'periods'
            period_date = datetime.strptime(period_str, '%Y-%m-%d').date()
            # Format label based on timeframe
            if timeframe == 'week': period_label = period_date.strftime('%d %b') # Start of week
            elif timeframe == 'month': period_label = period_date.strftime('%b %Y')
            elif timeframe == 'quarter': quarter = ((period_date.month - 1) // 3) + 1; period_label = f"Q{quarter} {period_date.year}"
            else: period_label = str(period_date.year)

            formatted_result.append({
                 'month': period_label, # Keep 'month' key for frontend compatibility?
                 'period_start': period_str,
                 'businesses': business_dict.get(period_str, 0),
                 'revenue': revenue_dict.get(period_str, 0.0)
            })
        return formatted_result


    @action(detail=False, methods=['get'])
    def growth(self, request):
        """Get growth trends based on timeframe"""
        if not request.user.has_perm('quickstart.view_business_metrics'):
            self.permission_denied(request, message="You do not have permission to view business growth metrics.")

        timeframe = request.query_params.get('timeframe', 'month')
        result = self._get_growth_data(timeframe)
        return Response(result)


    @action(detail=False, methods=['get'])
    def geographical(self, request):
        """Get geographical distribution of businesses"""
        if not request.user.has_perm('quickstart.view_business_metrics'):
            self.permission_denied(request, message="You do not have permission to view geographical business data.")

        view_type = request.query_params.get('view_type', 'province') # province or city
        data_type = request.query_params.get('data_type', 'count') # count, revenue, growth (province only)

        if view_type == 'province':
            group_by_fields = ['businessState']
        else: # city
            group_by_fields = ['businessCity', 'businessState']

        # Base annotation
        queryset = BusinessInfo.objects.values(*group_by_fields).annotate(
            count=Count('businessId'),
            total_revenue=Coalesce(Sum(
                 'classesmain__options__schedules__instances__bookings__amount_paid',
                 filter=Q(classesmain__options__schedules__instances__bookings__status__in=['confirmed', 'completed'])
            ), Value(0), output_field=DecimalField())
        )

        result_data = []
        growth_data = {} # For calculating growth

        # Calculate growth if requested (Province only)
        if data_type == 'growth' and view_type == 'province':
            today = timezone.now().date()
            year_ago = today - timedelta(days=365)
            # Year ago counts by province
            year_ago_counts = BusinessInfo.objects.filter(
                createdAt__date__lt=year_ago
            ).values('businessState').annotate(past_count=Count('businessId'))
            growth_data = {item['businessState']: item['past_count'] for item in year_ago_counts}


        # Process results
        for item in queryset:
            province = item['businessState']
            province_code = self.get_province_code(province)
            region = self.get_region_for_province(province)
            current_count = item['count']
            revenue = float(item['total_revenue'])
            growth_rate = 0

            if data_type == 'growth' and view_type == 'province':
                past_count = growth_data.get(province, 0)
                if past_count > 0:
                    growth_rate = ((current_count - past_count) / past_count) * 100
                elif current_count > 0:
                    growth_rate = 100 # Infinite growth effectively
                else:
                    growth_rate = 0

            if view_type == 'province':
                 result_data.append({
                     'province': province_code,
                     'count': current_count,
                     'revenue': revenue,
                     'growth': round(growth_rate, 1) if data_type == 'growth' else 0,
                     'region': region,
                     # Include full name for potential display
                     'province_full': province
                 })
            else: # city view
                 result_data.append({
                     'city': item['businessCity'], # Use city as primary identifier
                     'province': province_code, # Keep province code
                     'state_code': province_code, # Alias for consistency if needed
                     'count': current_count,
                     'revenue': revenue,
                     'region': region,
                     # Include full names for potential display
                     'province_full': province
                 })

        # Sort based on data_type
        sort_key = data_type if data_type != 'count' else 'count'
        if data_type == 'growth': sort_key = 'growth'
        if data_type == 'revenue': sort_key = 'revenue'

        result_data.sort(key=lambda x: x.get(sort_key, 0), reverse=True)

        # Limit results
        limit = 13 if view_type == 'province' else 10
        result_data = result_data[:limit]

        return Response(result_data)

    @action(detail=False, methods=['get'])
    def export(self, request):
        """Export businesses data as CSV"""
        if not request.user.has_perm('quickstart.export_business_data'):
             self.permission_denied(request, message="You do not have permission to export business data.")

        # Apply request filters to the queryset before exporting
        queryset = self.filter_queryset(self.get_queryset())

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="businesses_export.csv"'
        writer = csv.writer(response)

        # Define headers based on annotated/model fields available in get_queryset
        headers = [
            'Business ID', 'Business Name', 'Type', 'Category',
            'City', 'State', 'Calculated Status', 'Featured', 'Avg Rating',
            'Reviews Count', 'Bookings Count', 'Classes Count', 'Total Revenue',
            'Created At', 'Owner Email'
        ]
        writer.writerow(headers)

        # Fetch data efficiently using values_list
        # Ensure the order matches the headers precisely
        business_data = queryset.values_list(
            'businessId',         # 0
            'businessName',       # 1
            'businessType',       # 2
            'classCategory',      # 3
            'businessCity',       # 4
            'businessState',      # 5
            'calculated_status',  # 6 (Annotation)
            'featured',           # 7
            'rating',             # 8 (Annotation)
            'totalReviews',       # 9 (Model field - ensure it's accurate or re-annotate)
            'bookings_count',     # 10 (Annotation)
            'classes_count',      # 11 (Annotation)
            'revenue',            # 12 (Annotation)
            'createdAt',          # 13
            'owner__email'        # 14 (From select_related)
        )

        for business in business_data:
            row = [
                business[0],  # Business ID
                business[1],  # Business Name
                business[2],  # Type
                business[3],  # Category
                business[4],  # City
                business[5],  # State
                business[6],  # Calculated Status
                'Yes' if business[7] else 'No',  # Featured
                round(business[8] or 0, 1),  # Avg Rating
                business[9],  # Reviews Count
                business[10], # Bookings Count
                business[11], # Classes Count
                float(business[12] or 0),  # Total Revenue
                business[13].strftime('%Y-%m-%d %H:%M:%S') if business[13] else '',  # Created At
                business[14]  # Owner Email
            ]
            writer.writerow(row)

        return response

    @action(detail=False, methods=['post'])
    def announcements(self, request):
        """Send announcements to businesses"""
        if not request.user.has_perm('quickstart.send_business_announcements'):
            self.permission_denied(request, message="You do not have permission to send announcements.")

        # --- Input validation ---
        recipient_type = request.data.get('recipientType', 'all')
        title = request.data.get('title')
        message = request.data.get('message')
        urgency = request.data.get('urgency', 'normal') # Consider using this
        send_email = request.data.get('sendEmail', True)
        send_in_app = request.data.get('sendInApp', True) # Consider implementation

        if not title or not message:
            return Response(
                {'error': 'Title and message are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # --- Select recipient businesses ---
        businesses_qs = BusinessInfo.objects.all() # Start with all

        if recipient_type == 'active':
            businesses_qs = businesses_qs.filter(isActive=True)
        elif recipient_type == 'featured':
            businesses_qs = businesses_qs.filter(featured=True)
        elif recipient_type == 'new':
            thirty_days_ago = timezone.now() - timedelta(days=30)
            businesses_qs = businesses_qs.filter(createdAt__gte=thirty_days_ago)
        elif recipient_type == 'verified':
            businesses_qs = businesses_qs.filter(verificationStatus='verified')
        # Add more types if needed (e.g., specific category)

        # Get distinct owner emails efficiently
        owner_emails = list(businesses_qs.filter(owner__isnull=False, owner__email__isnull=False).values_list('owner__email', flat=True).distinct())

        # --- Actual implementation would involve background tasks ---
        if not owner_emails:
             return Response({
                'success': True, # Or False? Depends on expectation
                'message': "No recipients found for the selected criteria.",
                'recipient_count': 0,
            })

        logger.info(f"Announcement '{title}' triggered for {len(owner_emails)} business owners by Admin {request.user.email}")

        # Example: Use Celery or Django-Q
        # from your_tasks import send_announcement_task
        # send_announcement_task.delay(owner_emails, title, message, send_email, send_in_app)

        # --- Log Action ---
        # Optionally create an AuditLog entry for sending announcements

        return Response({
            'success': True,
            'message': f"Announcement task queued for {len(owner_emails)} recipients.",
            'recipient_count': len(owner_emails),
            'sent_emails': send_email,
            'sent_in_app': send_in_app # Reflects intent, not completion
        })

    # --- Helper methods ---
    def get_province_code(self, province_name):
        """Map province full name to code. Case-insensitive."""
        if not province_name: return '' # Handle None or empty string
        province_map = {
            'ontario': 'ON', 'quebec': 'QC', 'british columbia': 'BC',
            'alberta': 'AB', 'manitoba': 'MB', 'saskatchewan': 'SK',
            'nova scotia': 'NS', 'new brunswick': 'NB',
            'newfoundland and labrador': 'NL', 'prince edward island': 'PE',
            'northwest territories': 'NT', 'yukon': 'YT', 'nunavut': 'NU'
        }
        # Also map codes to themselves for safety
        code_map = {v: v for v in province_map.values()}
        province_map.update(code_map)

        cleaned_name = str(province_name).strip().lower()
        return province_map.get(cleaned_name, cleaned_name[:2].upper()) # Default to first 2 chars


    def get_region_for_province(self, province):
            """Map province (name or code) to region. Case-insensitive."""
            if not province: return 'Other' # Handle None or empty string
            # Map both full names and codes
            region_map = {
                # Full Names (lowercase)
                'ontario': 'Central', 'quebec': 'Eastern', 'british columbia': 'Western',
                'alberta': 'Western', 'manitoba': 'Central', 'saskatchewan': 'Central',
                'nova scotia': 'Atlantic', 'new brunswick': 'Atlantic',
                'newfoundland and labrador': 'Atlantic', 'prince edward island': 'Atlantic',
                'northwest territories': 'Northern', 'yukon': 'Northern', 'nunavut': 'Northern',
                # Codes (lowercase)
                'on': 'Central', 'qc': 'Eastern', 'bc': 'Western', 'ab': 'Western',
                'mb': 'Central', 'sk': 'Central', 'ns': 'Atlantic', 'nb': 'Atlantic',
                'nl': 'Atlantic', 'pe': 'Atlantic', 'nt': 'Northern', 'yt': 'Northern',
                'nu': 'Northern'
            }
            cleaned_province = str(province).strip().lower()
            return region_map.get(cleaned_province, 'Other')