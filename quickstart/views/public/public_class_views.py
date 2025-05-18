# quickstart/views/public/public_class_views.py

import math
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.db.models import (
    Q, Avg, Count, Min, Max, Value, F, Subquery, OuterRef,
    DecimalField, IntegerField, Sum, Case, When, ExpressionWrapper, FloatField, Prefetch
)
from django.db.models.functions import Coalesce, Cast, Power, Abs, Log
from django.contrib.postgres.search import SearchVector, SearchQuery, SearchRank
from django.utils import timezone
from decimal import Decimal, InvalidOperation
import logging
import requests
from urllib.parse import quote
from datetime import datetime, time, date as datetime_date # Import date separately to avoid conflict

from ...models import ClassesMain, ClassOption, Reviews, Booking, Schedule, ScheduleInstance
from ...serializers import PublicClassSerializer, ScheduleSerializer
from ..utils import haversine_distance

logger = logging.getLogger(__name__)

PHOTON_API_URL = "https://photon.komoot.io/api/"
DEFAULT_SEARCH_RADIUS_KM = 50

# --- Weights for Relevance Score (REQUIRES TUNING) ---
# These are conceptual "Normalized" weights, assuming input factors are scaled (mostly 0-1)
# The final score will be a sum of these weighted, scaled factors.
W_FEATURED_N = 500.0          # Max points from being featured (if featured is 0 or 1)
W_TEXT_RANK_N = 150.0         # Max points from text match (Postgres rank is ~0-1)
W_RATING_N = 100.0            # Max points from perfect rating (rating 0-5, scaled to 0-1)
W_REVIEW_COUNT_N = 60.0       # Max points from high review count (log-transformed & scaled)
W_DISTANCE_N = 120.0          # Max points from being very close (normalized distance proximity is 0-1)
W_NEWNESS_N = 80.0            # Max points from being very new (newness score scaled 0-1)

# --- Parameters for Scaling/Normalization ---
MAX_RATING_VALUE = 5.0
# Estimate max log(reviews+1) you expect. E.g., log10(1000+1) ~ 3.0. If a class has 10k reviews, it's ~4.
# This helps normalize the review count impact.
MAX_EXPECTED_LOG_REVIEWS = math.log10(1000 + 1) # Adjust based on typical max reviews
MAX_RECENCY_DAYS_FOR_BOOST = 60 # Classes newer than this get a boost
# RECENCY_DECAY_FACTOR ensures the boost decays to 0 over MAX_RECENCY_DAYS_FOR_BOOST
# This is essentially how many points are lost per day from W_NEWNESS_N
RECENCY_DECAY_PER_DAY = W_NEWNESS_N / MAX_RECENCY_DAYS_FOR_BOOST if MAX_RECENCY_DAYS_FOR_BOOST > 0 else 0


def geocode_location_text_backend(location_text):
    if not location_text:
        return None
    try:
        params = {'q': quote(location_text), 'limit': 1}
        headers = {'User-Agent': 'ClasseasilyApp/1.0 (Python Requests)'}
        response = requests.get(PHOTON_API_URL, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data and data.get('features') and len(data['features']) > 0:
            feature = data['features'][0]
            lon, lat = feature['geometry']['coordinates']
            props = feature['properties']
            name = props.get('name', '')
            street = props.get('street', '')
            housenumber = props.get('housenumber', '')
            city = props.get('city', props.get('town', props.get('village', '')))
            state = props.get('state', '')
            country = props.get('country', '')
            address_parts = []
            if name and name not in [street, city]:
                address_parts.append(name)
            if housenumber and street:
                address_parts.append(f"{housenumber} {street}")
            elif street:
                address_parts.append(street)
            if city: address_parts.append(city)
            if state: address_parts.append(state)
            if country: address_parts.append(country)
            full_display_name = ", ".join(filter(None, address_parts)) or location_text
            return float(lat), float(lon), full_display_name
    except requests.RequestException as e:
        logger.error(f"Backend geocoding HTTP error for '{location_text}': {e}")
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Error parsing geocoding response for '{location_text}': {e}")
    return None


class PublicClassViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = PublicClassSerializer
    # pagination_class = YourPaginationClass # Ensure pagination is set globally or here

    AVERAGE_RATING_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
        .values('classId').annotate(avg_rating=Avg('rating')).values('avg_rating')[:1],
        output_field=DecimalField(max_digits=3, decimal_places=1)
    )
    REVIEW_COUNT_SUBQUERY = Subquery(
        Reviews.objects.filter(classId=OuterRef('pk'), status='approved')
        .values('classId').annotate(count=Count('reviewId')).values('count')[:1],
        output_field=IntegerField()
    )
    MIN_PRICE_SUBQUERY = Subquery(
         Schedule.objects.filter(
             option__classId=OuterRef('pk'),
             is_active=True
         ).filter( # Filter for schedules that are currently bookable or ongoing
             Q(option__booking_type='Full Course', end_date__gte=timezone.now().date()) |
             Q(option__booking_type='Single Session', date__gte=timezone.now().date())
         )
         .order_by('price').values('price')[:1],
         output_field=DecimalField(max_digits=10, decimal_places=2)
    )
    MAX_PRICE_SUBQUERY = Subquery( # Apply similar future-date filtering for max_price
         Schedule.objects.filter(
             option__classId=OuterRef('pk'),
             is_active=True
         ).filter(
             Q(option__booking_type='Full Course', end_date__gte=timezone.now().date()) |
             Q(option__booking_type='Single Session', date__gte=timezone.now().date())
         )
         .order_by('-price').values('price')[:1],
         output_field=DecimalField(max_digits=10, decimal_places=2)
    )

    def get_queryset(self):
        return ClassesMain.objects.select_related(
            'businessId', 'category', 'subcategory'
        ).prefetch_related(
            'images',
            'options', 
            Prefetch( 
                'options__schedules',
                queryset=Schedule.objects.filter(is_active=True),
            ),
            Prefetch(  
                'options__schedules__instances'
            )
        ).filter(
            status='active',
            businessId__isActive=True,
            businessId__verificationStatus='verified'
        ).annotate(
            average_rating=Coalesce(self.AVERAGE_RATING_SUBQUERY, Value(Decimal('0.0'))),
            review_count=Coalesce(self.REVIEW_COUNT_SUBQUERY, Value(0)),
            min_price=Coalesce(self.MIN_PRICE_SUBQUERY, None), # Now reflects future prices
            max_price=Coalesce(self.MAX_PRICE_SUBQUERY, None), # Now reflects future prices
        ).distinct()
        
    @action(
        detail=True, methods=['post'], permission_classes=[IsAuthenticated],
        url_path='toggle-favorite', url_name='toggle-favorite'
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
            location_display_name = request.query_params.get('location') # Usually from selected geocoded location
            keyword = request.query_params.get('keyword')
            price_min_str = request.query_params.get('price_min')
            price_max_str = request.query_params.get('price_max')
            time_preferences = request.query_params.getlist('time_preference')
            days = request.query_params.getlist('days')
            class_type = request.query_params.get('class_type') # e.g. 'class', 'course', 'all'
            category_key = request.query_params.get('category_key')
            # subcategory_key = request.query_params.get('subcategory_key') # Add if needed
            req_date_str = request.query_params.get('date')
            req_participants_str = request.query_params.get('participants')
            sort_by = request.query_params.get('sort_by', 'relevance')

            search_lat, search_lng, search_radius_km = None, None, None
            if req_lat_str and req_lng_str:
                try:
                    search_lat = float(req_lat_str)
                    search_lng = float(req_lng_str)
                    search_radius_km = float(req_radius_km_str) if req_radius_km_str and float(req_radius_km_str) > 0 else DEFAULT_SEARCH_RADIUS_KM
                except (ValueError, TypeError): logger.warning(f"Invalid geo params received: lat='{req_lat_str}', lng='{req_lng_str}', radius='{req_radius_km_str}'")
            elif location_search_text:
                geocoded_result = geocode_location_text_backend(location_search_text)
                if geocoded_result:
                    search_lat, search_lng, _ = geocoded_result
                    search_radius_km = float(req_radius_km_str) if req_radius_km_str and float(req_radius_km_str) > 0 else DEFAULT_SEARCH_RADIUS_KM
                else:
                    if not location_display_name: location_display_name = location_search_text
            
            queryset = self.get_queryset()

            if keyword:
                search_query_obj = SearchQuery(keyword, search_type='websearch', config='english')
                queryset = queryset.annotate(
                    text_rank=SearchRank(F('search_vector'), search_query_obj)
                ).filter(search_vector=search_query_obj)
            else:
                queryset = queryset.annotate(text_rank=Value(0.0, output_field=FloatField()))


            if location_display_name and not (search_lat and search_lng):
                location_filter = Q()
                for term_part in location_display_name.split(','):
                    for term in term_part.strip().split():
                        term = term.lower()
                        if term: location_filter |= (Q(location__icontains=term) | Q(businessId__businessCity__icontains=term) | Q(businessId__businessState__icontains=term) | Q(businessId__businessZipCode__icontains=term))
                if location_filter: queryset = queryset.filter(location_filter).distinct()

            if price_min_str:
                try: queryset = queryset.filter(Q(min_price__gte=Decimal(price_min_str)) | Q(min_price__isnull=True)) # Allow classes with no price yet
                except InvalidOperation: logger.warning(f"Invalid price_min: {price_min_str}")
            if price_max_str:
                try: queryset = queryset.filter(Q(min_price__lte=Decimal(price_max_str)) | Q(min_price__isnull=True))
                except InvalidOperation: logger.warning(f"Invalid price_max: {price_max_str}")

            if time_preferences:
                time_ranges = {'Morning':(time(6,0),time(11,59,59)), 'Afternoon':(time(12,0),time(16,59,59)), 'Evening':(time(17,0),time(22,0))}
                time_q = Q()
                for p in [pref for pref in time_preferences if pref in time_ranges]:
                    s,e = time_ranges[p]
                    time_q |= Q(options__schedules__instances__time__gte=s, options__schedules__instances__time__lte=e, options__schedules__instances__status='scheduled', options__schedules__is_active=True)
                if time_q: queryset = queryset.filter(time_q).distinct()

            if days:
                day_map={'Monday':'Mon','Tuesday':'Tue','Wednesday':'Wed','Thursday':'Thu','Friday':'Fri','Saturday':'Sat','Sunday':'Sun'}
                s_days=[day_map[d] for d in days if d in day_map]
                if s_days: queryset=queryset.filter(Q(options__schedules__day__in=s_days)&Q(options__schedules__instances__status='scheduled')&Q(options__schedules__is_active=True)).distinct()

            if class_type == 'course': queryset = queryset.filter(options__booking_type='Full Course').distinct()
            elif class_type == 'session': queryset = queryset.filter(options__booking_type='Single Session').distinct()
            
            if category_key and category_key != 'all': queryset = queryset.filter(category__key=category_key)
            # if subcategory_key: queryset = queryset.filter(subcategory__key=subcategory_key) # Add if used

            target_date_obj = None
            q_for_instances = Q(options__schedules__instances__status='scheduled', options__schedules__is_active=True)
            if req_date_str:
                try:
                    target_date_obj = datetime.strptime(req_date_str, '%Y-%m-%d').date()
                    q_for_instances &= Q(options__schedules__instances__date=target_date_obj)
                except ValueError: 
                    logger.warning(f"Invalid date format: {req_date_str}, defaulting to future dates.")
                    q_for_instances &= Q(options__schedules__instances__date__gte=timezone.now().date())
            else:
                q_for_instances &= Q(options__schedules__instances__date__gte=timezone.now().date())
            
            if req_participants_str:
                try:
                    num_participants = int(req_participants_str)
                    if num_participants > 0:
                         q_for_instances &= Q(options__schedules__instances__max_participants__gte=num_participants)
                except ValueError: logger.warning(f"Invalid participant count: {req_participants_str}")
            
            queryset = queryset.filter(q_for_instances).distinct()


            distances_map = {}
            final_results_list = [] 

            if search_lat is not None and search_lng is not None:
                candidate_classes_for_distance = list(queryset.exclude(Q(coordinates__isnull=True) | Q(coordinates__exact='')))
                temp_list_with_distance = []
                for klass_item in candidate_classes_for_distance:
                    try:
                        item_lat_str, item_lon_str = klass_item.coordinates.split(',')
                        item_lat, item_lon = float(item_lat_str), float(item_lon_str)
                        distance_km = haversine_distance(search_lat, search_lng, item_lat, item_lon)
                        distances_map[klass_item.pk] = distance_km 
                        klass_item.distance_from_search = distance_km # For serializer or direct use
                        if search_radius_km is None or distance_km <= search_radius_km:
                            temp_list_with_distance.append(klass_item)
                    except (ValueError, TypeError, AttributeError) as e: 
                        logger.warning(f"Class {klass_item.pk} ({klass_item.title}) has invalid coordinates '{klass_item.coordinates}' or other error: {e}")
                        if search_radius_km is None: # If no radius, it might still be included if it matches other criteria
                             temp_list_with_distance.append(klass_item) # Decide if it should be added
                        distances_map[klass_item.pk] = float('inf') 
                        klass_item.distance_from_search = float('inf') 
                final_results_list = temp_list_with_distance
            else: 
                final_results_list = list(queryset)
                for item in final_results_list: # Ensure all items have distance_from_search attribute
                    item.distance_from_search = None 
                    distances_map[item.pk] = None


            if sort_by == 'relevance':
                today_date = timezone.now().date()
                for item in final_results_list:
                    score = 0.0
                    
                    # Featured Boost (Business level)
                    if item.businessId and item.businessId.featured: # Check if businessId exists
                        score += W_FEATURED_N

                    # Text Rank (already 0-1 from annotation or default 0)
                    score += float(getattr(item, 'text_rank', 0.0)) * W_TEXT_RANK_N

                    # Rating (normalize 0-MAX_RATING_VALUE to 0-1)
                    if MAX_RATING_VALUE > 0:
                        normalized_rating = float(item.average_rating) / MAX_RATING_VALUE
                        score += normalized_rating * W_RATING_N
                    
                    # Review Count (log transform, then normalize)
                    if item.review_count > 0 and MAX_EXPECTED_LOG_REVIEWS > 0:
                        log_reviews = math.log10(item.review_count + 1)
                        normalized_log_reviews = min(1.0, log_reviews / MAX_EXPECTED_LOG_REVIEWS)
                        score += normalized_log_reviews * W_REVIEW_COUNT_N
                    
                    # Distance (normalized_dist_proximity is 0-1)
                    dist_km = distances_map.get(item.pk)
                    if dist_km is not None and dist_km != float('inf') and search_lat is not None and search_lng is not None:
                        # Use effective_radius for normalization (could be DEFAULT_SEARCH_RADIUS_KM or user's choice)
                        effective_radius_for_score = search_radius_km if search_radius_km and search_radius_km > 0 else DEFAULT_SEARCH_RADIUS_KM
                        if effective_radius_for_score > 0:
                            # Proximity score: higher for closer items, 0 if at or beyond effective_radius
                            proximity_score_component = max(0, (effective_radius_for_score - dist_km) / effective_radius_for_score)
                            score += proximity_score_component * W_DISTANCE_N
                    
                    # Newness Boost (decaying)
                    if item.createdAt: # Ensure createdAt is not None
                        # Ensure item.createdAt is a date object if it's datetime
                        item_created_date = item.createdAt.date() if isinstance(item.createdAt, datetime) else item.createdAt
                        if isinstance(item_created_date, datetime_date): # Check type again after potential conversion
                            days_old = (today_date - item_created_date).days
                            if days_old >= 0 and days_old <= MAX_RECENCY_DAYS_FOR_BOOST and RECENCY_DECAY_PER_DAY > 0:
                                newness_boost_points = W_NEWNESS_N - (days_old * RECENCY_DECAY_PER_DAY)
                                score += max(0, newness_boost_points)
                        else:
                            logger.warning(f"Class {item.pk} has invalid createdAt type: {type(item.createdAt)}")


                    item.relevance_score_final = score
                
                final_results_list.sort(key=lambda x: getattr(x, 'relevance_score_final', 0.0), reverse=True)

            elif sort_by == 'distance':
                # Ensure distances_map has valid numbers or inf for sorting
                final_results_list.sort(key=lambda x: distances_map.get(x.pk, float('inf')))
            elif sort_by == 'price_asc':
                final_results_list.sort(key=lambda x: x.min_price if x.min_price is not None else float('inf'))
            elif sort_by == 'price_desc':
                final_results_list.sort(key=lambda x: x.min_price if x.min_price is not None else float('-inf'), reverse=True) # Corrected to min_price for consistency if max_price is not always available
            elif sort_by == 'rating':
                final_results_list.sort(key=lambda x: (float(x.average_rating), int(x.review_count)), reverse=True)
            elif sort_by == 'reviews':
                final_results_list.sort(key=lambda x: int(x.review_count), reverse=True)
            elif sort_by == 'newest':
                final_results_list.sort(key=lambda x: x.createdAt if x.createdAt else datetime.min, reverse=True) # Handle None createdAt

            # --- Pagination ---
            page = self.paginate_queryset(final_results_list) # DRF handles page slicing
            if page is not None:
                # Add distance to each item in the page for the serializer
                for item_in_page in page:
                    item_in_page.distance = distances_map.get(item_in_page.pk) 
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data) # DRF constructs paginated response

            # Fallback if pagination is not used (e.g., not configured or page size is larger than results)
            for item in final_results_list: # Add distance for non-paginated full list
                item.distance = distances_map.get(item.pk)
            serializer = self.get_serializer(final_results_list, many=True, context={'request': request})
            return Response({'results': serializer.data}) # Should ideally still conform to paginated structure

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
            start_date_obj = timezone.datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date_obj = timezone.datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if start_date_obj > end_date_obj:
                 return Response({"error": "Start date cannot be after end date."}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            ClassOption.objects.select_related('classId__businessId').get( # Just check existence
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
                date__range=(start_date_obj, end_date_obj),
                status='scheduled',
                schedule__is_active=True # Ensure parent schedule is active
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
                        'price': str(instance.price), # Ensure price is string for JSON
                    })
                # Clean up empty date keys if no available slots for that date
                if date_key in availability_data and not availability_data[date_key]:
                     del availability_data[date_key]
            
            return Response(availability_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"Error fetching availability for option {option_id}: {e}", exc_info=True)
            return Response({"error": "An error occurred while fetching availability."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)