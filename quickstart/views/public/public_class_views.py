from rest_framework import viewsets, filters, status # Added status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.db.models import Q, Avg, Count, Min, Max, Value, F, Subquery, OuterRef, DecimalField, IntegerField, Sum, Case, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal, InvalidOperation
import logging
from datetime import time

# Adjust imports based on your structure
from ...models import ClassesMain, ClassOption, Reviews, Booking, Schedule, ScheduleInstance 
# Import public serializer
from ...serializers import PublicClassSerializer, ScheduleSerializer
from ..utils import haversine_distance # Adjust path , if needed

logger = logging.getLogger(__name__)

class PublicClassViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides READ-ONLY access to public Class information.
    Filters to show only active, verified classes from active, verified businesses.
    """
    permission_classes = [AllowAny]
    serializer_class = PublicClassSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]

    # Define search/order fields for public view
    search_fields = [
        'title', 'description', 'location',
        'category__name', 'subcategory__name',
        'businessId__businessName',
        'options__title'
    ]
    ordering_fields = [
        'title', 'createdAt', 'average_rating', 'review_count',
        'businessId__businessName',
        'min_price', # Allow ordering by minimum price
        # 'distance' # Sorting by distance requires calculating it first in search
    ]
    ordering = ['-createdAt'] # Default order: newest first

    # --- Subqueries for Annotations ---
    # Use DecimalField for price/rating consistency
    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
        .values('classId')
        .annotate(avg_rating=Avg('rating'))
        .values('avg_rating')[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1) # Specify precision
    )
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
        .values('classId')
        .annotate(count=Count('reviewId'))
        .values('count')[:1],
        output_field=IntegerField()
    )
    MIN_PRICE_SUBQUERY = Subquery(
         Schedule.objects.filter(
              option__classId=OuterRef('pk'),
              is_active=True      
         ).order_by('price').values('price')[:1],
         output_field=DecimalField(max_digits=10, decimal_places=2)
    )

    def get_queryset(self):
        """Base queryset for public view, filtered and annotated."""
        return ClassesMain.objects.select_related(
            'businessId', 'category', 'subcategory'
        ).prefetch_related(
            'images', # Prefetch public images
            'options', # Prefetch public options
            'options__schedules' # Prefetch public schedule info
        ).filter(
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).annotate(
            average_rating=Coalesce(self.AVERAGE_RATING_SUBQUERY, Value(Decimal('0.0'))), # Default to Decimal
            review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
            min_price=Coalesce(self.MIN_PRICE_SUBQUERY, None) # Keep None if no price
        ).distinct()

    # --- Search Action ---
    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def search(self, request):
        """ Public search endpoint for classes. """
        try:
            # Get search parameters
            lat = request.query_params.get('lat')
            lng = request.query_params.get('lng')
            radius_km = request.query_params.get('radius') # Radius in KM
            location_query = request.query_params.get('location')
            keyword = request.query_params.get('keyword')

            # Get filter parameters
            price_min_str = request.query_params.get('price_min')
            price_max_str = request.query_params.get('price_max')
            time_preferences = request.query_params.getlist('time_preference') # e.g., 'Morning', 'Afternoon'
            days = request.query_params.getlist('days') # e.g., 'Monday', 'Tuesday'
            class_type = request.query_params.get('class_type') # 'course' or 'session'
            category_key = request.query_params.get('category_key') # Use category key for filtering

            # Convert radius safely
            try:
                 radius_km = float(radius_km) if radius_km is not None else None
            except (ValueError, TypeError):
                 radius_km = None # Ignore invalid radius

            # Start with base public queryset
            queryset = self.get_queryset() # Already filtered and annotated

            # Apply keyword search (uses filter_backends config, but can be explicit)
            if keyword:
                 queryset = queryset.filter(
                      Q(title__icontains=keyword) |
                      Q(description__icontains=keyword) |
                      Q(businessId__businessName__icontains=keyword) |
                      Q(category__name__icontains=keyword) | # Search by displayed name
                      Q(options__title__icontains=keyword)
                 ).distinct() # Add distinct after Q object filtering

            # Apply location text search
            if location_query:
                location_filter = Q()
                location_terms = location_query.split()
                for term in location_terms:
                    location_filter |= (
                        Q(location__icontains=term) |
                        Q(businessId__businessCity__icontains=term) |
                        Q(businessId__businessState__icontains=term) |
                        Q(businessId__businessZipCode__icontains=term)
                    )
                queryset = queryset.filter(location_filter)

            # Apply price filter (using annotated min_price)
            try:
                if price_min_str:
                    price_min = Decimal(price_min_str)
                    # Filter out classes where the minimum price is less than the requested minimum
                    # We already annotated min_price, filter where min_price >= price_min OR min_price is NULL
                    queryset = queryset.filter(Q(min_price__gte=price_min) | Q(min_price__isnull=True))
                if price_max_str:
                    price_max = Decimal(price_max_str)
                    # Filter out classes where the minimum price is greater than the requested maximum
                    queryset = queryset.filter(Q(min_price__lte=price_max) | Q(min_price__isnull=True))
            except (ValueError, InvalidOperation):
                 logger.warning(f"Invalid price filter values: min='{price_min_str}', max='{price_max_str}'")
                 # Optionally return error or ignore filter

            # Apply time preference filter
            if time_preferences:
                time_ranges = {
                    'Morning': (time(6, 0), time(11, 59, 59)),
                    'Afternoon': (time(12, 0), time(16, 59, 59)),
                    'Evening': (time(17, 0), time(22, 0)) # Example ranges
                }
                time_filter = Q()
                valid_prefs = [p for p in time_preferences if p in time_ranges] # Ensure valid prefs
                if valid_prefs:
                    for pref in valid_prefs:
                        start, end = time_ranges[pref]
                        # Find classes that have AT LEAST ONE active schedule in the time range
                        time_filter |= Q(
                            options__schedules__time__gte=start,
                            options__schedules__time__lte=end,
                            options__schedules__is_active=True,
                            options__active=True
                        )
                    queryset = queryset.filter(time_filter).distinct() # Add distinct after filter

            # Apply days filter
            if days:
                 day_map = {'Monday': 'Mon', 'Tuesday': 'Tue', 'Wednesday': 'Wed', 'Thursday': 'Thu', 'Friday': 'Fri', 'Saturday': 'Sat', 'Sunday': 'Sun'}
                 # Filter days provided, ensuring they are valid keys in map
                 short_days = [day_map[day] for day in days if day in day_map]
                 if short_days:
                      # Find classes that have AT LEAST ONE active schedule on these days
                      queryset = queryset.filter(
                          options__schedules__day__in=short_days,
                          options__schedules__is_active=True,
                          options__active=True
                      ).distinct() # Add distinct after filter

            # Apply class type filter
            if class_type:
                if class_type == 'course':
                    # Find classes that have AT LEAST ONE active 'Full Course' option
                    queryset = queryset.filter(options__booking_type='Full Course', options__active=True).distinct()
                elif class_type == 'session':
                     # Find classes that have AT LEAST ONE active 'Single Session' option
                    queryset = queryset.filter(options__booking_type='Single Session', options__active=True).distinct()

            # Apply category filter (using category key)
            if category_key and category_key != 'all': # Ignore 'all' key
                 queryset = queryset.filter(category__key=category_key)


            # --- Distance Filtering (Requires Python processing) ---
            final_results = []
            if lat and lng and radius_km is not None: # Ensure lat, lng, and radius are valid
                 try:
                      user_lat = float(lat)
                      user_lon = float(lng)

                      # Fetch candidate IDs and coordinates efficiently
                      candidates_data = queryset.exclude(
                          Q(coordinates__isnull=True) | Q(coordinates__exact='')
                      ).values_list('classId', 'coordinates')

                      valid_ids_in_radius = []
                      distances = {} # Store distances for potential sorting/display

                      for class_id, coords_str in candidates_data:
                           try:
                                item_lat, item_lon = map(float, coords_str.split(','))
                                distance = haversine_distance(user_lat, user_lon, item_lat, item_lon)
                                if distance <= radius_km:
                                     valid_ids_in_radius.append(class_id)
                                     distances[class_id] = distance
                           except (ValueError, TypeError, AttributeError):
                                logger.warning(f"Skipping class {class_id} due to invalid coordinates: {coords_str}")
                                continue

                      # Filter the main queryset by the valid IDs found within radius
                      queryset = queryset.filter(classId__in=valid_ids_in_radius)

                      # Get the final list of objects and add distance
                      final_results_list = list(queryset) # Execute query
                      for item in final_results_list:
                           item.distance = distances.get(item.classId) # Add distance

                      # Optional: Sort results by distance
                      final_results_list.sort(key=lambda x: x.distance if hasattr(x, 'distance') and x.distance is not None else float('inf'))
                      final_results = final_results_list

                 except (ValueError, TypeError):
                      logger.warning(f"Invalid coordinates or radius for distance filtering: lat={lat}, lng={lng}, radius={radius_km}")
                      final_results = list(queryset) # Fallback to non-distance filtered list
            else:
                 # No valid distance parameters, use the already filtered queryset
                 final_results = list(queryset) # Execute query

            # Apply pagination after all filtering and sorting
            # Note: Pagination should ideally happen before converting to list if using Django pagination classes
            # For simplicity here, we serialize the final list. Consider pagination class for large results.
            page = self.paginate_queryset(final_results) # Use DRF pagination if configured
            if page is not None:
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)

            # Fallback if pagination is not used or fails
            serializer = self.get_serializer(final_results, many=True, context={'request': request})
            return Response({'results': serializer.data}) # Keep structure consistent

        except Exception as e:
            logger.error(f"Public class search error: {str(e)}", exc_info=True)
            return Response({"error": "An error occurred during search."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class PublicScheduleViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides PUBLIC read-only access to schedule-related information,
    primarily for fetching availability.
    """
    permission_classes = [AllowAny]
    # Serializer for potential future list/retrieve actions on public schedules
    serializer_class = ScheduleSerializer
    # Base queryset filters for active schedules of active/verified classes/businesses
    queryset = Schedule.objects.filter(
        is_active=True,
        option__classId__status='active',
        option__classId__businessId__isActive=True,
        option__classId__businessId__verificationStatus='verified'
    ).select_related('option', 'option__classId', 'option__classId__businessId')

    @action(detail=False, methods=['get'], url_path='availability')
    def availability(self, request):
        """
        Fetches available schedule instances for a given option within a date range.

        Query Params:
            option_id (required): The ID of the ClassOption.
            start_date (required): Start date in YYYY-MM-DD format.
            end_date (required): End date in YYYY-MM-DD format.
            is_course (optional): Boolean flag (true/false), defaults to false. (Not strictly used in this basic implementation but kept for compatibility)
        """
        # 1. Get and Validate Parameters
        option_id_str = request.query_params.get('option_id')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')


        if not option_id_str or not option_id_str.isdigit():
            return Response({"error": "Valid 'option_id' is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not start_date_str or not end_date_str:
            return Response({"error": "'start_date' and 'end_date' are required."}, status=status.HTTP_400_BAD_REQUEST)

        option_id = int(option_id_str)

        try:
            start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if start_date > end_date:
                 return Response({"error": "Start date cannot be after end date."}, status=status.HTTP_400_BAD_REQUEST)
            # Optional: Prevent fetching too far in the past/future if needed
            # today = timezone.now().date()
            # if end_date < today:
            #     return Response({"error": "Cannot fetch availability for past dates."}, status=status.HTTP_400_BAD_REQUEST)

        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        # 2. Check Public Visibility of the Option
        try:
            option = ClassOption.objects.select_related(
                'classId__businessId' # For checking business status
            ).get(
                optionId=option_id,
                classId__status='active', # Class must be active
                classId__businessId__isActive=True, # Business must be active
                classId__businessId__verificationStatus='verified' # Business must be verified
            )
        except ClassOption.DoesNotExist:
            logger.warning(f"Public availability request for non-existent or non-public option ID: {option_id}")
            return Response({"error": "Requested class option not found or is not available."}, status=status.HTTP_404_NOT_FOUND)

        # 3. Fetch Available Schedule Instances
        try:
            instances = ScheduleInstance.objects.filter(
                schedule__option_id=option_id,
                date__range=(start_date, end_date),
                status='scheduled',      # Must be scheduled (not cancelled/completed)
                schedule__is_active=True # Parent schedule must be active
            ).annotate(
                # Calculate current confirmed bookings efficiently
                current_bookings_count=Coalesce(
                    Sum('bookings__participants', filter=Q(bookings__status='confirmed')),
                    0,
                    output_field=IntegerField()
                )
            ).select_related('schedule').order_by('date', 'time') # Order for predictable output

            # 4. Structure the Response Data
            availability_data = {}
            for instance in instances:
                date_key = instance.date.isoformat() # YYYY-MM-DD
                if date_key not in availability_data:
                    availability_data[date_key] = []

                available_spots = instance.max_participants - instance.current_bookings_count

                # Only include the instance if there are spots available
                if available_spots > 0:
                    availability_data[date_key].append({
                        'instance_id': instance.id,
                        'time': instance.time.strftime('%H:%M'), # HH:MM format
                        'duration': instance.duration,
                        'available_spots': available_spots,
                        'price': str(instance.price), # Ensure Decimal is serialized correctly
                        # Add other details if needed by frontend
                        # 'schedule_id': instance.schedule_id,
                    })
                # Optional: If a date ends up with no available slots, remove the key
                if date_key in availability_data and not availability_data[date_key]:
                     del availability_data[date_key]


            return Response(availability_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error fetching availability for option {option_id}: {e}", exc_info=True)
            return Response({"error": "An error occurred while fetching availability."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)