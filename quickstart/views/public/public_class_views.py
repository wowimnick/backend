# public_class_views.py

from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.db.models import (
    Q, Avg, Count, Min, Max, Value, F, Subquery, OuterRef,
    DecimalField, IntegerField, Sum, Case, When, Exists
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from decimal import Decimal, InvalidOperation
import logging
import requests
from urllib.parse import quote
from datetime import datetime, time # Import datetime

from ...models import ClassesMain, ClassOption, Reviews, Booking, Schedule, ScheduleInstance
from ...serializers import PublicClassSerializer, ScheduleSerializer
from ..utils import haversine_distance

logger = logging.getLogger(__name__)

PHOTON_API_URL = "https://photon.komoot.io/api/"
DEFAULT_SEARCH_RADIUS_KM = 50

def geocode_location_text_backend(location_text):
    if not location_text:
        return None
    try:
        params = {'q': quote(location_text), 'limit': 1}
        response = requests.get(PHOTON_API_URL, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data and data.get('features') and len(data['features']) > 0:
            feature = data['features'][0]
            lon, lat = feature['geometry']['coordinates']
            props = feature['properties']
            street_part = f"{props.get('housenumber', '')} {props.get('street', '')}".strip()
            city_part = props.get('city') or props.get('town') or props.get('village') or ''
            state_part = props.get('state') or ''
            country_part = props.get('country') or ''
            name_part = props.get('name') or ''
            
            display_parts = [name_part, street_part, city_part, state_part, country_part]
            # Filter out empty or identical consecutive parts for a cleaner name
            unique_display_parts = []
            last_part = None
            for part in display_parts:
                if part and part.strip() and part.strip() != last_part:
                    unique_display_parts.append(part.strip())
                    last_part = part.strip()
            
            full_display_name = ", ".join(unique_display_parts) if unique_display_parts else location_text
            return float(lat), float(lon), full_display_name
    except requests.RequestException as e:
        logger.error(f"Backend geocoding error for '{location_text}': {e}")
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Error parsing geocoding response for '{location_text}': {e}")
    return None


class PublicClassViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = PublicClassSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'title', 'description', 'location',
        'category__name', 'subcategory__name',
        'businessId__businessName',
        'options__title'
    ]
    ordering_fields = [
        'title', 'createdAt', 'average_rating', 'review_count',
        'businessId__businessName',
        'min_price',
    ]
    ordering = ['-createdAt']

    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
        .values('classId')
        .annotate(avg_rating=Avg('rating'))
        .values('avg_rating')[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1)
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
        return ClassesMain.objects.select_related(
            'businessId', 'category', 'subcategory'
        ).prefetch_related(
            'images', 
            'options__schedules__instances', # Prefetch instances for filtering
        ).filter(
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).annotate(
            average_rating=Coalesce(self.AVERAGE_RATING_SUBQUERY, Value(Decimal('0.0'))),
            review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
            min_price=Coalesce(self.MIN_PRICE_SUBQUERY, None)
        ).distinct()
        
    @action(
        detail=True, 
        methods=['post'],
        permission_classes=[IsAuthenticated],
        url_path='toggle-favorite',
        url_name='toggle-favorite'
    )
    def toggle_favorite(self, request, pk=None):
        klass = self.get_object()
        user = request.user
        try:
            if user.favorited.filter(pk=klass.pk).exists():
                user.favorited.remove(klass)
                is_favorited = False
            else:
                user.favorited.add(klass)
                is_favorited = True
            return Response({"status": "success", "is_favorited": is_favorited}, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error toggling favorite for user {user.email}, class {klass.pk}: {e}", exc_info=True)
            return Response({"error": "An internal error occurred."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def search(self, request):
        try:
            req_lat_str = request.query_params.get('lat')
            req_lng_str = request.query_params.get('lng')
            req_radius_km_str = request.query_params.get('radius')
            location_search_text = request.query_params.get('location_search')
            
            location_display_name = request.query_params.get('location')
            keyword = request.query_params.get('keyword')
            price_min_str = request.query_params.get('price_min')
            price_max_str = request.query_params.get('price_max')
            time_preferences = request.query_params.getlist('time_preference')
            days = request.query_params.getlist('days')
            class_type = request.query_params.get('class_type')
            category_key = request.query_params.get('category_key')
            
            req_date_str = request.query_params.get('date')
            req_participants_str = request.query_params.get('participants')

            search_lat, search_lng, search_radius_km = None, None, None

            if req_lat_str and req_lng_str:
                try:
                    search_lat = float(req_lat_str)
                    search_lng = float(req_lng_str)
                    search_radius_km = float(req_radius_km_str) if req_radius_km_str else DEFAULT_SEARCH_RADIUS_KM
                except (ValueError, TypeError):
                    logger.warning(f"Invalid lat/lng/radius: {req_lat_str}, {req_lng_str}, {req_radius_km_str}")
            elif location_search_text:
                geocoded_result = geocode_location_text_backend(location_search_text)
                if geocoded_result:
                    search_lat, search_lng, _ = geocoded_result
                    search_radius_km = float(req_radius_km_str) if req_radius_km_str else DEFAULT_SEARCH_RADIUS_KM
                else:
                    if not location_display_name: location_display_name = location_search_text
            
            queryset = self.get_queryset()

            if keyword:
                 queryset = queryset.filter(
                      Q(title__icontains=keyword) | Q(description__icontains=keyword) |
                      Q(businessId__businessName__icontains=keyword) |
                      Q(category__name__icontains=keyword) | Q(options__title__icontains=keyword)
                 ).distinct()

            if location_display_name and not (search_lat and search_lng):
                location_filter = Q()
                for term in location_display_name.split():
                    term = term.strip(',').lower()
                    if term:
                        location_filter |= (
                            Q(location__icontains=term) |
                            Q(businessId__businessCity__icontains=term) |
                            Q(businessId__businessState__icontains=term) |
                            Q(businessId__businessZipCode__icontains=term)
                        )
                if location_filter: queryset = queryset.filter(location_filter).distinct()

            if price_min_str:
                try: queryset = queryset.filter(Q(min_price__gte=Decimal(price_min_str)) | Q(min_price__isnull=True))
                except InvalidOperation: logger.warning(f"Invalid price_min: {price_min_str}")
            if price_max_str:
                try: queryset = queryset.filter(Q(min_price__lte=Decimal(price_max_str)) | Q(min_price__isnull=True))
                except InvalidOperation: logger.warning(f"Invalid price_max: {price_max_str}")
            
            if time_preferences:
                time_ranges = {'Morning': (time(6,0),time(11,59,59)), 'Afternoon': (time(12,0),time(16,59,59)), 'Evening': (time(17,0),time(22,0))}
                time_filter_q = Q()
                for pref in [p for p in time_preferences if p in time_ranges]:
                    start, end = time_ranges[pref]
                    time_filter_q |= Q(
                        options__schedules__instances__time__gte=start, 
                        options__schedules__instances__time__lte=end,
                        options__schedules__instances__status='scheduled', # Instance must be scheduled
                        options__schedules__is_active=True # Schedule must be active
                        # No options__active=True as ClassOption model doesn't have this field
                    )
                if time_filter_q: queryset = queryset.filter(time_filter_q).distinct()

            if days:
                 day_map = {'Monday':'Mon', 'Tuesday':'Tue', 'Wednesday':'Wed', 'Thursday':'Thu', 'Friday':'Fri', 'Saturday':'Sat', 'Sunday':'Sun'}
                 short_days = [day_map[day] for day in days if day in day_map]
                 if short_days: 
                     queryset = queryset.filter(
                         Q(options__schedules__day__in=short_days) & # Check the recurring day of the schedule
                         Q(options__schedules__instances__status='scheduled') & # Ensure there are scheduled instances
                         Q(options__schedules__is_active=True) # Ensure the schedule itself is active
                         # No options__active=True
                     ).distinct()

            if class_type:
                # Here, options__active=True was implicitly checking if the option existed.
                # We are querying for classes that HAVE options of a certain booking_type.
                # The get_queryset already ensures the ClassMain is active.
                if class_type == 'course': 
                    queryset = queryset.filter(options__booking_type='Full Course').distinct()
                elif class_type == 'session': 
                    queryset = queryset.filter(options__booking_type='Single Session').distinct()


            if category_key and category_key != 'all':
                 queryset = queryset.filter(category__key=category_key)

            target_date = None
            future_only_q = Q() # For applying future date constraint

            if req_date_str:
                try:
                    target_date = datetime.strptime(req_date_str, '%Y-%m-%d').date()
                    # Filter for classes having at least one active, scheduled instance on THIS SPECIFIC date
                    queryset = queryset.filter(
                        options__schedules__instances__date=target_date,
                        options__schedules__instances__status='scheduled',
                        options__schedules__is_active=True
                    ).distinct()
                    logger.info(f"Filtered by specific date: {target_date}")
                except ValueError:
                    logger.warning(f"Invalid date format for filtering: {req_date_str}")
                    # If date is invalid, maybe default to future only? Or ignore date filter.
                    # For now, let's assume if req_date_str is present but invalid, we don't filter by date.
            else:
                # NO specific date requested, so filter for instances from today onwards
                today = timezone.now().date()
                future_only_q = Q(
                    options__schedules__instances__date__gte=today,
                    options__schedules__instances__status='scheduled',
                    options__schedules__is_active=True
                )
                logger.info(f"No specific date; filtering for instances on or after {today}")
            
            # Apply participant filter (modified to incorporate future_only_q if applicable)
            if req_participants_str:
                try:
                    num_participants = int(req_participants_str)
                    if num_participants > 0:
                        instance_capacity_q = Q(
                            options__schedules__instances__max_participants__gte=num_participants,
                            options__schedules__instances__status='scheduled', # Already part of future_only_q
                            options__schedules__is_active=True # Already part of future_only_q
                        )
                        if target_date: # If a specific date was successfully parsed
                            instance_capacity_q &= Q(options__schedules__instances__date=target_date)
                        elif future_only_q: # If no specific date, apply participant check to future instances
                             instance_capacity_q &= future_only_q # Combine with future date constraint
                        
                        # If neither target_date nor future_only_q (meaning date was invalid and we didn't default to future)
                        # then instance_capacity_q just checks capacity on any scheduled/active instance.
                        # To be more robust, if future_only_q is not empty, it should be the base for this.

                        if future_only_q and not target_date: # Apply participant filter ON TOP of future instances
                            queryset = queryset.filter(future_only_q & instance_capacity_q).distinct()
                        elif target_date: # Apply participant filter on top of specific date instances (already filtered by date)
                            queryset = queryset.filter(instance_capacity_q).distinct() 
                        else: # Fallback if no date/future constraint, just apply capacity
                            queryset = queryset.filter(instance_capacity_q).distinct()

                        logger.info(f"Filtered by participants: {num_participants}")
                except ValueError:
                    logger.warning(f"Invalid participant count for filtering: {req_participants_str}")
            elif future_only_q: # No participant filter, but no specific date, so apply future_only_q
                queryset = queryset.filter(future_only_q).distinct()
            
            final_results_list = []
            if search_lat is not None and search_lng is not None and search_radius_km is not None:
                 logger.info(f"Performing distance filtering: lat={search_lat}, lng={search_lng}, radius={search_radius_km}km")
                 candidates_data = queryset.exclude(
                     Q(coordinates__isnull=True) | Q(coordinates__exact='')
                 ).values_list('classId', 'coordinates')
                 valid_ids_in_radius = []
                 distances_map = {} 
                 for class_id, coords_str in candidates_data:
                      try:
                           item_lat_str, item_lon_str = coords_str.split(',')
                           item_lat, item_lon = float(item_lat_str), float(item_lon_str)
                           distance = haversine_distance(search_lat, search_lng, item_lat, item_lon)
                           if distance <= search_radius_km:
                                valid_ids_in_radius.append(class_id)
                                distances_map[class_id] = distance
                      except (ValueError, TypeError, AttributeError) as e:
                           logger.warning(f"Skipping class {class_id} due to invalid coordinates '{coords_str}': {e}")
                           continue
                 queryset = queryset.filter(classId__in=valid_ids_in_radius)
                 temp_list = list(queryset) # Execute query
                 for item in temp_list: item.distance = distances_map.get(item.classId) 
                 temp_list.sort(key=lambda x: x.distance if hasattr(x, 'distance') and x.distance is not None else float('inf'))
                 final_results_list = temp_list
            else:
                 final_results_list = list(queryset) # Execute query

            page = self.paginate_queryset(final_results_list)
            if page is not None:
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(final_results_list, many=True, context={'request': request})
            return Response({'results': serializer.data})

        except Exception as e:
            logger.error(f"Public class search error: {str(e)}", exc_info=True)
            return Response({"error": "An error occurred during search."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class PublicScheduleViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = ScheduleSerializer
    queryset = Schedule.objects.filter(
        is_active=True,
        option__classId__status='active',
        option__classId__businessId__isActive=True,
        option__classId__businessId__verificationStatus='verified'
    ).select_related('option', 'option__classId', 'option__classId__businessId')

    @action(detail=False, methods=['get'], url_path='availability')
    def availability(self, request):
        option_id_str = request.query_params.get('option_id')
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')

        if not (option_id_str and option_id_str.isdigit()):
            return Response({"error": "Valid 'option_id' is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not (start_date_str and end_date_str):
            return Response({"error": "'start_date' and 'end_date' are required."}, status=status.HTTP_400_BAD_REQUEST)
        
        option_id = int(option_id_str)
        try:
            start_date = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if start_date > end_date:
                 return Response({"error": "Start date cannot be after end date."}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            option = ClassOption.objects.select_related('classId__businessId').get(
                optionId=option_id,
                classId__status='active',
                classId__businessId__isActive=True,
                classId__businessId__verificationStatus='verified'
            )
        except ClassOption.DoesNotExist:
            return Response({"error": "Requested class option not found or is not available."}, status=status.HTTP_404_NOT_FOUND)

        try:
            instances = ScheduleInstance.objects.filter(
                schedule__option_id=option_id,
                date__range=(start_date, end_date),
                status='scheduled',
                schedule__is_active=True
            ).annotate(
                current_bookings_count=Coalesce(
                    Sum('bookings__participants', filter=Q(bookings__status='confirmed')),
                    0,
                    output_field=IntegerField()
                )
            ).select_related('schedule').order_by('date', 'time')

            availability_data = {}
            for instance in instances:
                date_key = instance.date.isoformat()
                if date_key not in availability_data:
                    availability_data[date_key] = []
                
                available_spots = instance.max_participants - instance.current_bookings_count
                if available_spots > 0:
                    availability_data[date_key].append({
                        'instance_id': instance.id,
                        'time': instance.time.strftime('%H:%M'),
                        'duration': instance.duration,
                        'available_spots': available_spots,
                        'price': str(instance.price),
                    })
                if date_key in availability_data and not availability_data[date_key]:
                     del availability_data[date_key]
            
            return Response(availability_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error fetching availability for option {option_id}: {e}", exc_info=True)
            return Response({"error": "An error occurred while fetching availability."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)