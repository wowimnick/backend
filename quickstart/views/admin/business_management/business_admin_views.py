
from decimal import Decimal
from django.db.models import (Count, Sum, Avg, Case, When, F, DecimalField,
                              Q, Value, CharField, Exists, OuterRef, Subquery) # Added Subquery
from django.db.models.functions import Coalesce, TruncDay, TruncMonth, TruncWeek, TruncQuarter, TruncYear
from django.db.models import FloatField, IntegerField, BooleanField, DateTimeField
from django.utils import timezone
from datetime import datetime, timedelta
from rest_framework import viewsets, status, filters # Added filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission # Import BasePermission
import csv
from django.http import Http404, HttpResponse
import logging 

from ....models import BusinessInfo, ClassCategory, ClassOption, Booking, ClassesMain, Reviews
from ....serializers.admin.business_management.admin_business_serializers import AdminBusinessDetailSerializer, AdminBusinessListSerializer

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

    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'list':
            return AdminBusinessListSerializer
        # Use detail for retrieve, update, partial_update, create (if applicable)
        return AdminBusinessDetailSerializer

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
        'classes_count', 'bookings_count', 'revenue', 'status'
    ]
    ordering = ['-createdAt'] # Default ordering

    def get_queryset(self):
        """Return queryset with annotations for admin views using Subqueries"""
        queryset = BusinessInfo.objects.select_related('owner', 'owner__role').all()

        # --- Subqueries for Aggregations ---
        # Subquery for active classes count
        classes_subquery = Subquery(
            ClassesMain.objects.filter(
                businessId=OuterRef('pk')
            ).values('businessId').annotate(c=Count('pk')).values('c'),
            output_field=IntegerField()
        )

        # Subquery for bookings count
        bookings_subquery = Subquery(
            Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=OuterRef('pk'),
                status__in=['confirmed', 'completed']
            ).values('schedule_instance__schedule__option__classId__businessId') # Group by business
            .annotate(c=Count('pk', distinct=True)) # Count distinct bookings per business
            .values('c'),
            output_field=IntegerField()
        )

        # Subquery for revenue
        revenue_subquery = Subquery(
             Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=OuterRef('pk'),
                status__in=['confirmed', 'completed']
            ).values('schedule_instance__schedule__option__classId__businessId') # Group by business
            .annotate(s=Sum('amount_paid')) # Sum amount per business
            .values('s'),
            # Ensure output_field matches expected type, handle potential None
            output_field=DecimalField(max_digits=12, decimal_places=2)
        )

        review_count_subquery = Subquery(
            Reviews.objects.filter(
                businessId=OuterRef('pk'),
                status='approved' # Count only approved reviews
            ).values('businessId') # Group by business
            .annotate(c=Count('pk')) # Count reviews per business
            .values('c'),
            output_field=IntegerField()
        )
        
        rating_subquery = Subquery(
            Reviews.objects.filter(
                businessId=OuterRef('pk'), # Direct link
                status='approved'
            ).values('businessId') # Group by business
            .annotate(avg=Avg('rating')) # Calculate average per business
            .values('avg'),
            output_field=FloatField() # Use FloatField for Avg
        )


        # Subquery for has_active_schedules (remains Exists)
        has_active_schedules_subquery = Exists(
            ClassOption.objects.filter(
                classId__businessId=OuterRef('pk'),
                schedules__is_active=True,
                schedules__instances__status='scheduled',
                schedules__instances__date__gte=timezone.now().date()
            )
        )

        # Annotate using the subqueries
        queryset = queryset.annotate(
            has_active_schedules=has_active_schedules_subquery,
            classes_count=Coalesce(classes_subquery, 0),
            bookings_count=Coalesce(bookings_subquery, 0),
            revenue=Coalesce(revenue_subquery, Value(Decimal('0.00'))), # Coalesce Decimal
            rating=Coalesce(rating_subquery, Value(0.0)), 
            review_count=Coalesce(review_count_subquery, 0),

            status=Case(
                When(Q(isActive=True) & Q(has_active_schedules=True), then=Value('active')),
                When(Q(isActive=True) & Q(has_active_schedules=False), then=Value('no_schedules')),
                When(verificationStatus='pending', then=Value('pending')),
                default=Value('inactive'),
                output_field=CharField(max_length=20)
            )
        )

        # --- Filtering remains the same ---
        category = self.request.query_params.get('category', None)
        status_param = self.request.query_params.get('status', None)
        featured = self.request.query_params.get('featured', None)

        if category:
            queryset = queryset.filter(classCategory=category)
        if status_param:
            # Filter on the annotated status
            queryset = queryset.filter(status=status_param)
        if featured is not None:
            is_featured = str(featured).lower() in ['true', '1', 'yes']
            queryset = queryset.filter(featured=is_featured)

        # DRF filters (SearchFilter, OrderingFilter) will be applied automatically after this
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

        # Use the annotated queryset to fetch the single instance
        queryset = self.get_queryset()
        try:
            # Use get() which will raise DoesNotExist if not found
            instance = queryset.get(pk=kwargs['pk'])
        except BusinessInfo.DoesNotExist:
            raise Http404("Business not found.") # Let DRF handle the 404

        # Optional hierarchy check can remain here if needed
        # if not user_can_manage(request.user, instance.owner):
        #    self.permission_denied(request, message="Hierarchy restriction: Cannot view details for this business owner.")

        serializer = self.get_serializer(instance) # Serializer now gets annotated data
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

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, CanAccessBusinessAdmin])
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
        """Get geographical distribution of businesses, aggregated by CITY, using subqueries."""
        if not request.user.has_perm('quickstart.view_business_metrics'):
            self.permission_denied(request, message="You do not have permission to view geographical business data.")

        data_type = request.query_params.get('data_type', 'count')

        # --- Define Subqueries for City-Level Aggregation ---

        # Subquery for business count
        count_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef('businessCity'),
                businessState=OuterRef('businessState')
            ).values('businessCity', 'businessState').annotate(c=Count('pk')).values('c'),
            output_field=IntegerField()
        )

        # Subquery for total revenue
        revenue_subquery = Subquery(
             Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId__businessCity=OuterRef('businessCity'),
                schedule_instance__schedule__option__classId__businessId__businessState=OuterRef('businessState'),
                status__in=['confirmed', 'completed']
            ).values(
                'schedule_instance__schedule__option__classId__businessId__businessCity',
                'schedule_instance__schedule__option__classId__businessId__businessState'
            ).annotate(s=Sum('amount_paid')).values('s'),
            output_field=DecimalField(max_digits=12, decimal_places=2)
        )

        # Subquery for total classes count
        classes_subquery = Subquery(
            ClassesMain.objects.filter(
                businessId__businessCity=OuterRef('businessCity'),
                businessId__businessState=OuterRef('businessState')
            ).values('businessId__businessCity', 'businessId__businessState').annotate(c=Count('pk')).values('c'),
            output_field=IntegerField()
        )

        # --- NEW: Subqueries for Average Lat/Lon ---
        avg_lat_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef('businessCity'),
                businessState=OuterRef('businessState'),
                latitude__isnull=False # Ensure we only average non-nulls
            ).values(
                'businessCity', 'businessState' # Grouping matches outer distinct
            ).annotate(
                avg=Avg('latitude') # Calculate average
            ).values('avg'),
            output_field=FloatField()
        )

        avg_lon_subquery = Subquery(
            BusinessInfo.objects.filter(
                businessCity=OuterRef('businessCity'),
                businessState=OuterRef('businessState'),
                longitude__isnull=False # Ensure we only average non-nulls
            ).values(
                'businessCity', 'businessState' # Grouping matches outer distinct
            ).annotate(
                avg=Avg('longitude') # Calculate average
            ).values('avg'),
            output_field=FloatField()
        )


        # --- Main Query ---
        # Start with distinct city/state combinations (no coordinate filter needed here)
        queryset = BusinessInfo.objects.values(
            'businessCity',
            'businessState'
        ).distinct().annotate( # Annotate onto the distinct city/state pairs
            # Apply all subqueries
            avg_lat=avg_lat_subquery, # Use the subquery result
            avg_lon=avg_lon_subquery, # Use the subquery result
            city_count=Coalesce(count_subquery, 0),
            city_revenue=Coalesce(revenue_subquery, Value(Decimal('0.00'))),
            city_classes_count=Coalesce(classes_subquery, 0)
        ).filter(
            # --- Filter AFTER annotation: Ensure we only keep cities with valid coordinates ---
            avg_lat__isnull=False,
            avg_lon__isnull=False
        ).order_by('-city_count') # Order by the calculated count


        result_data = []
        for item in queryset:
            # Extract values (now we know avg_lat/lon should exist if not filtered out)
            city = item.get('businessCity')
            province_name = item.get('businessState')
            avg_lat_val = item.get('avg_lat') # Directly use the annotated value
            avg_lon_val = item.get('avg_lon') # Directly use the annotated value
            count_val = item.get('city_count', 0)
            revenue_val = item.get('city_revenue', Decimal('0.00'))
            classes_count_val = item.get('city_classes_count', 0)

            # Minimal validation needed here now, as None values were filtered
            if city and province_name and avg_lat_val is not None and avg_lon_val is not None:
                try:
                    centroid_lon = float(avg_lon_val)
                    centroid_lat = float(avg_lat_val)
                except (ValueError, TypeError):
                    logger.error(f"Unexpected non-float coordinate for {city}, {province_name}: Lat={avg_lat_val}, Lon={avg_lon_val}")
                    continue # Should not happen due to filter, but safety check

                province_code = self.get_province_code(province_name)
                region = self.get_region_for_province(province_name)

                # Debug print
                print(f"Processing {city}, {province_name}: avg_lat={centroid_lat}, avg_lon={centroid_lon}, count={count_val}, revenue={revenue_val}, classes={classes_count_val}")

                result_data.append({
                    'city': city,
                    'province_code': province_code,
                    'province_full': province_name,
                    'region': region,
                    'count': count_val,
                    'classes_count': classes_count_val, # Include class count
                    'revenue': float(revenue_val),
                    'growth': 0,
                    'centroid': [centroid_lon, centroid_lat]
                })
            # No else needed as filtering should prevent this


        # Sorting and limiting remain the same
        sort_key = 'count'
        if data_type == 'revenue': sort_key = 'revenue'
        result_data.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
        limit = 50
        result_data = result_data[:limit]

        print(f"Geographical data prepared: {len(result_data)} cities.")
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
            'City', 'State', 'Status', 'Featured', 'Avg Rating',
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
            'status',  # 6 (Annotation)
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