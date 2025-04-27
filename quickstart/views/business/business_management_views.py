from rest_framework import viewsets, status, permissions, generics, views
from rest_framework.decorators import action, api_view, permission_classes, parser_classes
from rest_framework.response import Response
from django.db.models import Q, Sum, Count, Avg
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models.functions import Coalesce
from django.db.models import IntegerField, Subquery, OuterRef, F
from rest_framework.views import APIView
from datetime import timedelta
from datetime import datetime
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError as DRFValidationError
from rest_framework.parsers import MultiPartParser, FormParser
import logging

from ...models import (
    BusinessInfo, Booking, ClassesMain, Reviews, ScheduleInstance, Payment, CustomUser
)
from ...serializers import (
    ManagedBusinessInfoSerializer,
    BusinessStatsSerializer,
    BusinessRegistrationSerializer,
    BusinessDashboardOverviewSerializer,
    colors
)

from ...utils.permissions import (
    CanAccessBusinessDashboard,
    CanManageOwnBusinessProfile,
    CanDeleteOwnBusinessProfile,
)

logger = logging.getLogger(__name__)

# --- Business Registration ---

@api_view(['POST'])
@permission_classes([IsAuthenticated]) # User must be logged in
@parser_classes([MultiPartParser, FormParser])
def register_business(request):
    """
    Handles the creation of a new BusinessInfo instance by an authenticated user.
    Uses BusinessRegistrationSerializer which now handles 4 steps including agreements.
    (URL: /api/business/register/)
    """
    # Serializer context handles associating the user as owner.
    try:
        # Pass request.data and context to the updated serializer
        serializer = BusinessRegistrationSerializer(
            data=request.data,
            context={'request': request}
        )
        # Validate all data (including agreements, parsed lists, etc.)
        serializer.is_valid(raise_exception=True)
        # Save the business instance (owner, verificationStatus set internally)
        business = serializer.save()
        logger.info(f"Business '{business.businessName}' (ID: {business.businessId}) registered by user {request.user.email} (4-step flow completed).")
        return Response({
            'status': 'success',
            'message': 'Business registered successfully!', # Simplified message
            'businessId': business.businessId
        }, status=status.HTTP_201_CREATED)

    except DRFValidationError as e:
         # Log validation errors from any step
         logger.warning(f"Business registration validation failed for user {request.user.email}. Errors: {e.detail}")
         return Response({
            'status': 'error',
            'errors': e.detail # Return detailed validation errors
         }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Error in business registration for user {request.user.email}: {str(e)}", exc_info=True)
        return Response({
            'status': 'error',
            'errors': {'message': 'An unexpected error occurred during registration.'}
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# --- Views for Logged-in Business Users ---

@api_view(['GET'])
@permission_classes([IsAuthenticated]) # User must be logged in
def get_user_businesses(request):
    """
    Get businesses the current logged-in user owns or manages.
    (URL: /api/my-businesses/)
    """
    user = request.user
    # Fetch businesses this user owns or manages
    businesses = BusinessInfo.objects.filter(
        Q(owner=user) | Q(managers=user)
    ).distinct().select_related('owner').order_by('businessName') # Optimization and ordering

    # Use the standard serializer, sensitive data is handled by context/permissions elsewhere
    serializer = ManagedBusinessInfoSerializer(businesses, many=True, context={'request': request})
    return Response(serializer.data)

class MyBusinessOverviewView(APIView):
    """
    Provides the aggregated overview dashboard data for the
    single business associated with the currently authenticated user.
    """
    permission_classes = [IsAuthenticated, CanAccessBusinessDashboard] # Apply permission check

    def get_business_for_user(self, user):
        """Helper to find the single business for the user."""
        businesses = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user))
        count = businesses.count()

        if count == 0:
            raise NotFound("No business profile associated with this user found.")
        elif count > 1:
            # This violates the stated constraint, indicates a data issue or logic flaw elsewhere
            logger.error(f"User {user.email} is associated with multiple businesses ({count}). Constraint violated.")
            raise PermissionDenied("Error: Multiple business associations found. Please contact support.")
        business = businesses.first()

        # Explicitly check object-level permission if the class permission doesn't handle it implicitly
        # This might be redundant if CanAccessBusinessDashboard already implies ownership/management
        # self.check_object_permissions(self.request, business) # Requires DRF setup if not in ViewSet

        return business

    def get(self, request, *args, **kwargs):
        """Handles GET request to fetch the overview data."""
        user = request.user
        try:
            business = self.get_business_for_user(user)
        except (NotFound, PermissionDenied) as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND if isinstance(e, NotFound) else status.HTTP_403_FORBIDDEN)

        pk = business.pk # Get the primary key for logging/internal use

        # --- Define Time Periods ---
        # Make RevenueAnalyticsView import local to the method if it causes issues at class level
        from ...views import RevenueAnalyticsView # Moved import here

        now = timezone.now()
        today = now.date()
        start_current_month = today.replace(day=1)
        next_month = (start_current_month + timedelta(days=32)).replace(day=1)
        end_current_month = next_month - timedelta(days=1)

        start_previous_month = (start_current_month - timedelta(days=1)).replace(day=1)
        end_previous_month = start_current_month - timedelta(days=1)

        thirty_days_ago = now - timedelta(days=30)

        # --- Fetch Data Efficiently ---
        revenue_view = RevenueAnalyticsView()
        revenue_view.request = request

        # --- Revenue Calculation (Current & Previous for % Change) ---
        try:
            current_revenue_metrics = revenue_view.calculate_metrics(
                business,
                timezone.make_aware(datetime.combine(start_current_month, datetime.min.time())),
                timezone.make_aware(datetime.combine(end_current_month, datetime.max.time()))
            )
            # Previous month revenue calculated within calculate_metrics now
            monthly_revenue = {
                'value': current_revenue_metrics['total_revenue'],
                'change': current_revenue_metrics['revenue_growth'] # Use the calculated growth
            }
            revenue_trend_data = revenue_view.get_revenue_trends(
                business, thirty_days_ago, now
            )
        except Exception as e:
            logger.error(f"Error calculating revenue/trends for overview (Business {pk}): {e}", exc_info=True)
            monthly_revenue = {'value': 0, 'change': 0}
            revenue_trend_data = []

        # --- Other Metrics ---

        # Total Students (Current & Previous for % Change)
        try:
            current_students_count = CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business,
                bookings__booking_date__date__range=[start_current_month, end_current_month] # Based on bookings THIS month
            ).distinct().count()

            previous_students_count = CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business,
                bookings__booking_date__date__range=[start_previous_month, end_previous_month] # Based on bookings LAST month
            ).distinct().count()

            student_change = 0.0
            if previous_students_count > 0:
                student_change = round(((current_students_count - previous_students_count) / previous_students_count) * 100, 1)

            total_students = {'value': current_students_count, 'change': student_change}
        except Exception as e:
             logger.error(f"Error calculating student metrics for overview (Business {pk}): {e}", exc_info=True)
             total_students = {'value': 0, 'change': 0}


        # Active Classes (Snapshot - Change kept at 0)
        try:
            active_classes_count = business.classesmain_set.filter(status='active').count()
            # Change calculation is complex/ambiguous for a snapshot, keep as 0
            active_classes = {'value': active_classes_count, 'change': 0}
        except Exception as e:
            logger.error(f"Error calculating active classes for overview (Business {pk}): {e}", exc_info=True)
            active_classes = {'value': 0, 'change': 0}


        # Average Rating (Current & Previous for Absolute Change)
        try:
            # Current avg rating uses all approved reviews (as before)
            current_avg_rating_val = business.reviews_set.filter(
                status='approved'
            ).aggregate(avg=Avg('rating'))['avg'] or 0.0
            current_avg_rating = round(current_avg_rating_val, 1)

            # Previous avg rating based on reviews CREATED last month
            previous_avg_rating_val = Reviews.objects.filter(
                classId__businessId=business,
                status='approved',
                createdAt__date__range=[start_previous_month, end_previous_month] # Filter by creation date
            ).aggregate(avg=Avg('rating'))['avg'] or 0.0
            previous_avg_rating = round(previous_avg_rating_val, 1)

            # Calculate absolute change
            rating_change = round(current_avg_rating - previous_avg_rating, 1)

            average_rating = {'value': current_avg_rating, 'change': rating_change}
        except Exception as e:
            logger.error(f"Error calculating rating metrics for overview (Business {pk}): {e}", exc_info=True)
            average_rating = {'value': 0, 'change': 0}

        # --- Upcoming Classes ---
        try:
            upcoming_instances_qs = ScheduleInstance.objects.filter(
                schedule__option__classId__businessId=business, # Corrected
                date__gte=today,
                date__lte=today + timedelta(days=7),
                status='scheduled'
            ).select_related(
                'schedule__option'
            ).annotate(
                current_bookings_count=Coalesce(
                    Subquery(
                        Booking.objects.filter(
                            schedule_instance=OuterRef('pk'),
                            status='confirmed'
                        ).values('schedule_instance').annotate(
                            total_pax=Sum('participants')
                        ).values('total_pax')[:1]
                    ), 0, output_field=IntegerField()
                )
            ).order_by('date', 'time')[:5]

            upcoming_classes_data = []
            for inst in upcoming_instances_qs:
                # Format time safely for cross-platform
                formatted_time_str = ""
                try:
                    # Use #I for non-padded hour on Windows
                    formatted_time_str = inst.time.strftime('%#I:%M %p')
                except ValueError:
                    # Fallback for systems that might not support #I
                    hour = inst.time.strftime('%I') # Get potentially zero-padded hour
                    if hour.startswith('0'): hour = hour[1:] # Remove leading zero
                    formatted_time_str = inst.time.strftime(f'{hour}:%M %p')

                upcoming_classes_data.append({
                    'name': f"{inst.schedule.option.title}",
                    'time': f"{inst.date.strftime('%b %d')}, {formatted_time_str}", # Corrected time format
                    'current_occupancy': inst.current_bookings_count,
                    'max_occupancy': inst.max_participants,
                })

        except Exception as e:
             logger.error(f"Error getting upcoming classes for overview (Business {pk}): {e}", exc_info=True)
             # Ensure upcoming_classes_data is an empty list on error - THIS LINE WAS MISSING/FIXED
             upcoming_classes_data = []


        # --- Popular Classes ---
        try:
            popular_classes_raw = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business, # Corrected
            ).values(
                'schedule_instance__schedule__option__classId__title'
            ).annotate(
                enrollment=Count('id')
            ).order_by('-enrollment')[:5]

            popular_classes_data = [
                {'name': entry['schedule_instance__schedule__option__classId__title'], 'enrollment': entry['enrollment']}
                for entry in popular_classes_raw if entry['schedule_instance__schedule__option__classId__title']
            ]
        except Exception as e:
             logger.error(f"Error getting popular classes for overview (Business {pk}): {e}", exc_info=True)
             popular_classes_data = [] # Defined on error

        # --- Recent Activity ---
        recent_activity_data = []
        try:
            # Get recent bookings (confirmed)
            recent_bookings = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business, # Corrected
                booking_date__gte=now - timedelta(days=3),
                status = 'confirmed'
            ).select_related('user').order_by('-booking_date')[:3]
            for booking in recent_bookings:
                 recent_activity_data.append({
                     'timestamp': booking.booking_date,
                     'message': f"New enrollment: {booking.user.first_name} {booking.user.last_name}",
                     'icon': 'UserPlus',
                     'color': colors['chart']['blue']
                 })

            # Get recent payments (succeeded)
            recent_payments = Payment.objects.filter(
                booking__schedule_instance__schedule__option__classId__businessId=business, # Corrected
                status='succeeded',
                created_at__gte=now - timedelta(days=3)
            ).order_by('-created_at')[:3]
            for payment in recent_payments:
                 recent_activity_data.append({
                      'timestamp': payment.created_at,
                      'message': f"Payment received: ${payment.amount:.2f}",
                      'icon': 'DollarSign',
                      'color': colors['chart']['green']
                 })

            # Get recent reviews (approved)
            recent_reviews = Reviews.objects.filter(
                 classId__businessId=business,
                 status='approved',
                 createdAt__gte=now - timedelta(days=3)
            ).select_related('userId').order_by('-createdAt')[:3]
            for review in recent_reviews:
                 recent_activity_data.append({
                     'timestamp': review.createdAt,
                     'message': f"New review: {review.rating}★ from {review.userId.first_name}",
                     'icon': 'Star',
                     'color': colors['chart']['orange']
                 })

            # Sort and format
            recent_activity_data.sort(key=lambda x: x['timestamp'], reverse=True)
            recent_activity_data = recent_activity_data[:5]

            # Format timestamp for display (Windows compatible)
            for activity in recent_activity_data:
                try:
                    # Use #I for non-padded hour on Windows
                    activity['time'] = timezone.localtime(activity['timestamp']).strftime('%#I:%M %p, %b %d') # Corrected time format
                except ValueError:
                    # Fallback for systems that might not support #I
                    dt_local = timezone.localtime(activity['timestamp'])
                    hour = dt_local.strftime('%I')
                    if hour.startswith('0'): hour = hour[1:]
                    activity['time'] = dt_local.strftime(f'{hour}:%M %p, %b %d')

                del activity['timestamp']

        except Exception as e:
             logger.error(f"Error generating recent activity for overview (Business {pk}): {e}", exc_info=True)
             recent_activity_data = [] # Defined on error

        # --- Prepare Payload ---
        payload = {
            'metrics': {
                'total_students': total_students,
                'active_classes': active_classes,
                'monthly_revenue': monthly_revenue,
                'average_rating': average_rating,
            },
            'revenue_trend': revenue_trend_data,
            'upcoming_classes': upcoming_classes_data, # Now guaranteed to exist
            'popular_classes': popular_classes_data,
            'recent_activity': recent_activity_data
        }

        serializer = BusinessDashboardOverviewSerializer(payload)
        return Response(serializer.data)
    
class BusinessDashboardViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Provides Business Dashboard related statistics actions for the user's associated businesses.
    Requires 'access_business_dashboard' permission and ownership/management.
    (URL Base: /api/business-stats/)
    """
    serializer_class = BusinessStatsSerializer
    permission_classes = [IsAuthenticated, CanAccessBusinessDashboard] # Checks perm and ownership

    def get_queryset(self):
        """
        Ensures users only see stats for businesses they own or manage.
        """
        user = self.request.user
        return BusinessInfo.objects.filter(
            Q(owner=user) | Q(managers=user)
        ).distinct()
        
    @action(detail=True, methods=['get'])
    def dashboard_stats(self, request, pk=None):
        """ Get core stats for the business dashboard. """
        business = self.get_object() # Applies get_queryset and permission object check
        serializer = self.get_serializer(business, context={'request': request}) # Pass instance to serializer
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def revenue_over_time(self, request, pk=None):
        """ Get revenue trend data for the business. """
        business = self.get_object() # Permission/ownership checked
        timeframe = request.query_params.get('timeframe', 'monthly')
        # Use business.pk directly for filtering
        bookings_qs = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business.pk,
            status='completed',
            payment_status='paid'
        )
        if timeframe == 'daily':
            days = 30
            start_date = timezone.now().date() - timedelta(days=days) # Use date() for filtering GTE date field
            revenue_data = bookings_qs.filter(
                booking_date__date__gte=start_date # Filter by date part of booking_date
            ).values('booking_date__date').annotate(
                revenue=Sum('amount_paid')
            ).order_by('booking_date__date')
            response_data = [{'date': item['booking_date__date'].isoformat(), 'revenue': float(item['revenue'] or 0)} for item in revenue_data]
        else: # Monthly
            months = 12
            # Calculate start date more accurately for months
            start_date = (timezone.now().date().replace(day=1) - timedelta(days=1)).replace(day=1) # Start of previous month
            for _ in range(months - 1):
                 start_date = (start_date - timedelta(days=1)).replace(day=1)

            revenue_data = bookings_qs.filter(
                booking_date__date__gte=start_date
            ).values('booking_date__year', 'booking_date__month').annotate(
                revenue=Sum('amount_paid')
            ).order_by('booking_date__year', 'booking_date__month')
            response_data = [{'month': f"{item['booking_date__year']}-{item['booking_date__month']:02d}", 'revenue': float(item['revenue'] or 0)} for item in revenue_data]

        return Response(response_data)


    @action(detail=True, methods=['get'])
    def class_performance(self, request, pk=None):
        """ Get performance metrics per class for this business. """
        business = self.get_object() # Permission/ownership checked
        classes_qs = ClassesMain.objects.filter(
            businessId=business # Use the fetched business object directly
        ).annotate(
             booking_count=Count(
                'options__schedules__instances__bookings',
                filter=Q(options__schedules__instances__bookings__status='completed',
                         options__schedules__instances__bookings__payment_status='paid'),
                distinct=True
            ),
            revenue=Sum(
                'options__schedules__instances__bookings__amount_paid',
                filter=Q(options__schedules__instances__bookings__status='completed',
                         options__schedules__instances__bookings__payment_status='paid')
            ),
            review_count=Count('reviews', filter=Q(reviews__status='approved'), distinct=True),
            average_rating=Avg('reviews__rating', filter=Q(reviews__status='approved'))
        ).values(
            'classId', 'title', 'booking_count', 'revenue', 'review_count', 'average_rating'
        ).order_by('-revenue') # Example ordering

        response_data = [
            {
                'classId': item['classId'],
                'title': item['title'],
                'booking_count': item['booking_count'] or 0,
                'revenue': float(item['revenue'] or 0.0),
                'review_count': item['review_count'] or 0,
                'average_rating': round(float(item['average_rating'] or 0.0), 1)
            } for item in classes_qs
        ]
        return Response(response_data)

class MyBusinessProfileView(generics.RetrieveUpdateDestroyAPIView):
    """
    Allows authenticated business owners/managers to view, update, or delete
    their OWN associated BusinessInfo profile.
    (URL: /api/my-business/profile/) - No PK needed in URL
    """
    serializer_class = ManagedBusinessInfoSerializer
    parser_classes = [MultiPartParser, FormParser] # Handle image uploads via FormData
    permission_classes = [permissions.IsAuthenticated, CanManageOwnBusinessProfile]

    def get_object(self):
        """
        Fetches the single BusinessInfo object associated with the requesting user.
        """
        user = self.request.user
        try:
            # Use filter().first() for safety, handles cases where user might somehow
            # be linked to multiple, though ideally this is prevented elsewhere.
            business = BusinessInfo.objects.filter(
                 Q(owner=user) | Q(managers=user)
            ).select_related('owner').prefetch_related('managers').first() # Add prefetch/select_related

            if not business:
                 logger.warning(f"No BusinessInfo found for user {user.email} accessing my-business/profile/")
                 raise NotFound("No business profile associated with this user found.")

            # Manually check object permissions AFTER fetching, as RetrieveUpdateAPIView expects it
            self.check_object_permissions(self.request, business)
            return business

        except BusinessInfo.DoesNotExist: # Should be caught by filter().first() but good practice
            logger.warning(f"BusinessInfo.DoesNotExist unexpectedly raised for user {user.email} accessing my-business/profile/")
            raise NotFound("No business profile associated with this user found.")
        # MultipleObjectsReturned shouldn't happen with filter().first()

    def get_permissions(self):
        """ Set specific permission for DELETE """
        if self.request.method == 'DELETE':
            # Ensure user is authenticated and passes the stricter CanDeleteOwnBusinessProfile check
            return [IsAuthenticated(), CanDeleteOwnBusinessProfile()]
        # For GET/PUT/PATCH, rely on the class-level permissions checked by DRF + get_object
        return super().get_permissions()

    def perform_update(self, serializer):
        # Prevent changing owner or managers via this endpoint for security/simplicity
        serializer.validated_data.pop('owner', None)
        serializer.validated_data.pop('managers', None)
        serializer.validated_data.pop('verificationStatus', None)
        serializer.validated_data.pop('stripe_account_id', None)
        serializer.validated_data.pop('stripe_account_status', None)

        try:
            # <<< serializer.save() NOW ONLY USES FIELDS DEFINED IN THE UPDATED SERIALIZER >>>
            business = serializer.save()
            logger.info(f"Business Profile '{business.businessName}' (ID: {business.pk}) updated by user {self.request.user.email}")
        except Exception as e:
             logger.error(f"Error during perform_update for Business Profile (User: {self.request.user.email}): {str(e)}", exc_info=True)
             raise DRFValidationError("An error occurred while updating the business profile.")

    def perform_destroy(self, instance):
        # Permissions already checked by get_permissions -> CanDeleteOwnBusinessProfile
        business_name = instance.businessName
        business_id = instance.businessId

        # Handle potential image file deletion before deleting the record
        if instance.businessImage:
            try:
                instance.businessImage.delete(save=False)
            except Exception as e:
                logger.warning(f"Could not delete businessImage file for {business_id}: {e}")
        if instance.verificationDocument:
             try:
                 instance.verificationDocument.delete(save=False)
             except Exception as e:
                 logger.warning(f"Could not delete verificationDocument file for {business_id}: {e}")


        instance.delete() # Hard delete
        logger.warning(f"Business Profile '{business_name}' (ID: {business_id}) DELETED by owner {self.request.user.email}")