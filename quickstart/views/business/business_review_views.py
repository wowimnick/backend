# quickstart/views/business/business_review_views.py
import pytz
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from django.db.models import Q, F, Count, Avg
from django.db.models.functions import Coalesce, Round, TruncDate
from datetime import timedelta, datetime
from django.utils import timezone
import logging

from ...models import Reviews, BusinessInfo
from ...serializers.business.business_review_serializers import BusinessReviewSerializer
# from ...utils.email_utils import send_review_response_notification_email # Placeholder

logger = logging.getLogger(__name__)

# --- Permissions ---
class CanManageOwnBusinessReviews(BasePermission):
    message = "You do not have permission to manage reviews for this business."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return (
            user.has_perm('quickstart.view_own_business_reviews') and
            BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).exists()
        )

    def has_object_permission(self, request, view, obj): # obj is the Review instance
        user = request.user
        business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
        if not business:
            return False
        return obj.classId and obj.classId.businessId == business

# --- ViewSet ---
class BusinessReviewViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessReviewSerializer
    permission_classes = [IsAuthenticated, CanManageOwnBusinessReviews]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'comment', 'userId__first_name', 'userId__last_name', 'userId__email',
        'classId__title', 'rating', 'status'
    ]
    ordering_fields = ['createdAt', 'rating', 'status', 'classId__title', 'reported_at', 'responded_at']
    ordering = ['-createdAt']
    http_method_names = ['get', 'post', 'head', 'options']

    def get_business_context(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).first()
            if not business:
                 raise PermissionDenied("User not associated with any managed business.")
            return business
        except BusinessInfo.DoesNotExist:
             raise PermissionDenied("Associated business not found.")

    def get_queryset(self):
        business = self.get_business_context()
        # Ensure reviews are linked via classId to the business
        return Reviews.objects.filter(
            classId__businessId=business
        ).select_related(
            'userId', 'classId', 'booking'
        ).distinct()

    @action(detail=False, methods=['get'], url_path='analytics')
    def analytics(self, request):
        business = self.get_business_context()

        # Date range filtering for analytics (optional, but good for trends)
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        
        # Default to last 30 days if no range provided
        if not start_date_str or not end_date_str:
            end_date_naive = timezone.localdate() # Use local date for default range end
            start_date_naive = end_date_naive - timedelta(days=29)
        else:
            try:
                start_date_naive = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date_naive = datetime.strptime(end_date_str, '%Y-%m-%d').date()
                if start_date_naive > end_date_naive:
                    raise DRFValidationError("Start date cannot be after end date.")
            except ValueError:
                raise DRFValidationError("Invalid date format. Please use YYYY-MM-DD.")

        # Make dates timezone-aware for DB query (assuming createdAt is timezone-aware)
        # For simplicity, using UTC for the date range boundaries for DB query.
        # If createdAt is naive, adjust accordingly.
        start_datetime_utc = timezone.make_aware(datetime.combine(start_date_naive, datetime.min.time()), pytz.utc)
        end_datetime_utc = timezone.make_aware(datetime.combine(end_date_naive, datetime.max.time()), pytz.utc)
        
        # Base queryset for reviews within the business and date range
        reviews_qs = Reviews.objects.filter(
            classId__businessId=business,
            createdAt__range=[start_datetime_utc, end_datetime_utc] # Filter by review creation date
        )

        # --- Key Metrics ---
        total_reviews = reviews_qs.count()
        average_rating_data = reviews_qs.aggregate(avg_rating=Avg('rating'))
        average_rating = round(average_rating_data['avg_rating'], 1) if average_rating_data['avg_rating'] else 0.0
        
        responded_count = reviews_qs.filter(business_response__isnull=False, business_response__exact=False, business_response__gt='').count()
        response_rate = (responded_count / total_reviews * 100) if total_reviews > 0 else 0.0
        
        reviews_under_review = reviews_qs.filter(status='under_review').count()
        reviews_reported = reviews_qs.filter(reported=True).count()

        # --- Rating Distribution ---
        rating_distribution_data = reviews_qs.values('rating').annotate(count=Count('rating')).order_by('rating')
        rating_distribution_map = {item['rating']: item['count'] for item in rating_distribution_data}
        rating_distribution = [
            {'name': f'{i} Star{"s" if i > 1 else ""}', 'count': rating_distribution_map.get(i, 0)}
            for i in range(1, 6)
        ]

        # --- Response Status Distribution (Simplified) ---
        response_stats = [
            {'name': 'Responded', 'value': responded_count},
            {'name': 'Not Responded', 'value': total_reviews - responded_count}
        ]

        # --- Reviews Over Time (Example: Daily new reviews in the period) ---
        # Convert business's local time to UTC for querying createdAt
        # Business timezone needed here to correctly group by local business day
        business_pytz = pytz.timezone(business.business_timezone or 'UTC')
        
        daily_reviews_data = reviews_qs.annotate(
            date_local=TruncDate(F('createdAt'), tzinfo=business_pytz) # Group by local date of review creation
        ).values('date_local').annotate(
            new_reviews=Count('reviewId')
        ).order_by('date_local')

        # Create a complete list of dates in the range (local business time)
        all_trend_dates_local = {}
        current_scan_local_date = start_date_naive # Already a date object
        while current_scan_local_date <= end_date_naive:
            date_iso = current_scan_local_date.isoformat()
            all_trend_dates_local[date_iso] = {
                "date": date_iso, # Local date
                "new_reviews": 0,
            }
            current_scan_local_date += timedelta(days=1)
        
        for trend in daily_reviews_data:
            local_date_iso = trend['date_local'].isoformat()
            if local_date_iso in all_trend_dates_local:
                all_trend_dates_local[local_date_iso]['new_reviews'] = trend['new_reviews']
        
        processed_trends = sorted(all_trend_dates_local.values(), key=lambda x: x['date'])


        analytics_data = {
            'summary_metrics': {
                'total_reviews_in_period': total_reviews,
                'average_rating_in_period': average_rating,
                'response_rate_in_period': round(response_rate, 1),
                'reviews_under_review': reviews_under_review,
                'reviews_reported': reviews_reported,
            },
            'rating_distribution': rating_distribution,
            'response_status_distribution': response_stats,
            'reviews_over_time': processed_trends,
            'date_range': {
                'start': start_date_naive.isoformat(),
                'end': end_date_naive.isoformat(),
            }
        }
        return Response(analytics_data)
    
    @action(detail=True, methods=['post'], url_path='respond')
    def respond(self, request, pk=None):
        review = self.get_object() # Permission check done by get_object
        if not request.user.has_perm('quickstart.add_business_review_response'):
            raise PermissionDenied("You do not have permission to respond to reviews.")

        response_text = request.data.get('business_response', None)
        if response_text is None: # Allow empty string to clear response
             raise DRFValidationError({'business_response': 'This field is required (can be empty to clear).'})
        
        # Ensure response_text is a string, even if empty
        response_text_stripped = str(response_text).strip()

        if len(response_text_stripped) > 1000: # Max length for response
              raise DRFValidationError({'business_response': 'Response cannot exceed 1000 characters.'})

        old_response = review.business_response
        review.business_response = response_text_stripped if response_text_stripped else None # Store None if empty
        
        update_fields = ['business_response']
        if review.business_response != old_response: # Only update responded_at if response changed
            review.responded_at = timezone.now() if review.business_response else None
            update_fields.append('responded_at')

        review.save(update_fields=update_fields)
        logger.info(f"Business user {request.user.email} responded to Review {pk}.")

        # Placeholder for sending email notification to the student
        # if review.userId and review.business_response:
        #     try:
        #         send_review_response_notification_email(review.userId, review)
        #         logger.info(f"Review response notification email task initiated for review {pk} to user {review.userId.email}.")
        #     except Exception as email_error:
        #         logger.error(f"Failed to send review response notification for review {pk}: {email_error}", exc_info=True)

        serializer = self.get_serializer(review)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='report')
    def report(self, request, pk=None):
        review = self.get_object() # Permission check
        report_reason = request.data.get('report_reason', '').strip()
        if not report_reason:
            raise DRFValidationError({'report_reason': 'A reason for reporting is required.'})
        if len(report_reason) > 500: # Max length for reason
             raise DRFValidationError({'report_reason': 'Report reason cannot exceed 500 characters.'})

        if review.reported:
            return Response({'message': 'This review has already been reported.'}, status=status.HTTP_400_BAD_REQUEST)

        review.reported = True
        review.report_reason = report_reason
        review.reported_at = timezone.now()
        review.status = 'under_review' # Automatically change status
        review.save(update_fields=['reported', 'report_reason', 'reported_at', 'status'])
        logger.info(f"Review {pk} reported by business user {request.user.email}. Reason: {report_reason}")

        # Placeholder for notifying admins
        # send_admin_review_reported_notification(review)

        serializer = self.get_serializer(review)
        return Response(serializer.data)
        
    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)