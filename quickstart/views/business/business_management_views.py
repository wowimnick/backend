from rest_framework import viewsets, status, permissions, generics, views
from rest_framework.decorators import (
    action,
    api_view,
    permission_classes,
    parser_classes,
)
from rest_framework.response import Response
from django.utils import timezone
from django.db.models.functions import Coalesce
from django.db.models import Q, Sum, Count, Avg, Subquery, OuterRef, IntegerField, F, Value
from rest_framework.views import APIView
from datetime import timedelta
from datetime import datetime
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import (
    PermissionDenied,
    NotFound,
    ValidationError as DRFValidationError,
)
from rest_framework.parsers import MultiPartParser, FormParser
import pytz
import logging

from ...models import (
    BusinessInfo,
    Booking,
    ClassOption,
    ClassesMain,
    Reviews,
    Schedule,
    ScheduleInstance,
    Payment,
    CustomUser,
)

from .revenue_analytics_views import RevenueAnalyticsView
from ...serializers import (
    ManagedBusinessInfoSerializer,
    BusinessStatsSerializer,
    BusinessRegistrationSerializer,
    BusinessDashboardOverviewSerializer,
    colors,
)

from ...utils.permissions import (
    CanAccessBusinessDashboard,
    CanManageOwnBusinessProfile,
    CanDeleteOwnBusinessProfile,
)

logger = logging.getLogger(__name__)

# --- Business Registration ---


@api_view(["POST"])
@permission_classes([IsAuthenticated])  # User must be logged in
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
            data=request.data, context={"request": request}
        )
        # Validate all data (including agreements, parsed lists, etc.)
        serializer.is_valid(raise_exception=True)
        # Save the business instance (owner, verificationStatus set internally)
        business = serializer.save()
        logger.info(
            f"Business '{business.businessName}' (ID: {business.businessId}) registered by user {request.user.email} (4-step flow completed)."
        )
        return Response(
            {
                "status": "success",
                "message": "Business registered successfully!",  # Simplified message
                "businessId": business.businessId,
            },
            status=status.HTTP_201_CREATED,
        )

    except DRFValidationError as e:
        # Log validation errors from any step
        logger.warning(
            f"Business registration validation failed for user {request.user.email}. Errors: {e.detail}"
        )
        return Response(
            {
                "status": "error",
                "errors": e.detail,  # Return detailed validation errors
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        logger.error(
            f"Error in business registration for user {request.user.email}: {str(e)}",
            exc_info=True,
        )
        return Response(
            {
                "status": "error",
                "errors": {
                    "message": "An unexpected error occurred during registration."
                },
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# --- Views for Logged-in Business Users ---


@api_view(["GET"])
@permission_classes([IsAuthenticated])  # User must be logged in
def get_user_businesses(request):
    """
    Get businesses the current logged-in user owns or manages.
    (URL: /api/my-businesses/)
    """
    user = request.user
    # Fetch businesses this user owns or manages
    businesses = (
        BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user))
        .distinct()
        .select_related("owner")
        .order_by("businessName")
    )  # Optimization and ordering

    # Use the standard serializer, sensitive data is handled by context/permissions elsewhere
    serializer = ManagedBusinessInfoSerializer(
        businesses, many=True, context={"request": request}
    )
    return Response(serializer.data)


class MyBusinessOverviewView(APIView):
    permission_classes = [IsAuthenticated, CanAccessBusinessDashboard]

    def get_business_for_user(self, user):
        businesses = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user))
        count = businesses.count()
        if count == 0:
            raise NotFound("No business profile associated with this user found.")
        elif count > 1:
            logger.error(f"User {user.email} is associated with multiple businesses ({count}). Constraint violated.")
            raise PermissionDenied("Error: Multiple business associations found. Please contact support.")
        return businesses.first()

    def get(self, request, *args, **kwargs):
        user = request.user
        try:
            business = self.get_business_for_user(user)
        except (NotFound, PermissionDenied) as e:
            return Response(
                {'error': str(e)},
                status=(status.HTTP_404_NOT_FOUND if isinstance(e, NotFound) else status.HTTP_403_FORBIDDEN)
            )

        pk = business.pk # business primary key
        now_utc = timezone.now()
        today_utc_date = now_utc.date()
        seven_days_ago_utc_date = today_utc_date - timedelta(days=6) 

        current_month_start_naive_date = today_utc_date.replace(day=1)
        next_month_start_naive_date = (current_month_start_naive_date + timedelta(days=32)).replace(day=1)
        current_month_end_naive_date = next_month_start_naive_date - timedelta(days=1)
        previous_month_start_naive_date = (current_month_start_naive_date - timedelta(days=1)).replace(day=1)
        previous_month_end_naive_date = current_month_start_naive_date - timedelta(days=1)
        thirty_days_ago_utc_dt_start_of_day = (now_utc - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)

        revenue_view = RevenueAnalyticsView()

        # --- Revenue Metrics ---
        try:
            current_month_start_dt_aware = timezone.make_aware(datetime.combine(current_month_start_naive_date, datetime.min.time()), pytz.utc)
            current_month_end_dt_aware = timezone.make_aware(datetime.combine(current_month_end_naive_date, datetime.max.time()), pytz.utc)
            current_revenue_metrics = revenue_view.calculate_metrics(business, current_month_start_dt_aware, current_month_end_dt_aware)
            monthly_revenue = {"value": current_revenue_metrics["total_gross_revenue"], "change": current_revenue_metrics["revenue_growth"]}
            revenue_trend_data = revenue_view.get_revenue_trends(business, thirty_days_ago_utc_dt_start_of_day, now_utc)
        except Exception as e:
            logger.error(f"Error calculating revenue/trends for overview (Business {pk}): {e}", exc_info=True)
            monthly_revenue = {"value": 0, "change": 0}; revenue_trend_data = []

        # --- Student Metrics ---
        try:
            current_month_start_dt_aware_for_students = timezone.make_aware(datetime.combine(current_month_start_naive_date, datetime.min.time()), pytz.utc)
            current_month_end_dt_aware_for_students = timezone.make_aware(datetime.combine(current_month_end_naive_date, datetime.max.time()), pytz.utc)
            previous_month_start_dt_aware_for_students = timezone.make_aware(datetime.combine(previous_month_start_naive_date, datetime.min.time()), pytz.utc)
            previous_month_end_dt_aware_for_students = timezone.make_aware(datetime.combine(previous_month_end_naive_date, datetime.max.time()), pytz.utc)

            current_students_count = CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business,
                bookings__booking_date__range=[current_month_start_dt_aware_for_students, current_month_end_dt_aware_for_students]
            ).distinct().count()
            previous_students_count = CustomUser.objects.filter(
                bookings__schedule_instance__schedule__option__classId__businessId=business,
                bookings__booking_date__range=[previous_month_start_dt_aware_for_students, previous_month_end_dt_aware_for_students]
            ).distinct().count()
            student_change = 0.0
            if previous_students_count > 0: student_change = round(((current_students_count - previous_students_count) / previous_students_count) * 100, 1)
            elif current_students_count > 0: student_change = 100.0
            total_students = {"value": current_students_count, "change": student_change}
        except Exception as e:
            logger.error(f"Error calculating student metrics for overview (Business {pk}): {e}", exc_info=True)
            total_students = {"value": 0, "change": 0}

        # --- Active Classes Metric ---
        try:
            active_classes_count = business.classes.filter(status="active").count() # Uses related_name 'classes' from ClassesMain
            active_classes = {"value": active_classes_count, "change": 0} 
        except Exception as e:
            logger.error(f"Error calculating active classes for overview (Business {pk}): {e}", exc_info=True)
            active_classes = {"value": 0, "change": 0}

        # --- Average Rating Metric ---
        try:
            current_avg_rating_val = Reviews.objects.filter(classId__businessId=business, status="approved").aggregate(avg=Avg("rating"))["avg"] or 0.0
            current_avg_rating = round(current_avg_rating_val, 1)
            previous_month_start_dt_aware_for_reviews = timezone.make_aware(datetime.combine(previous_month_start_naive_date, datetime.min.time()), pytz.utc)
            previous_month_end_dt_aware_for_reviews = timezone.make_aware(datetime.combine(previous_month_end_naive_date, datetime.max.time()), pytz.utc)
            previous_avg_rating_val = Reviews.objects.filter(
                classId__businessId=business, status="approved",
                createdAt__range=[previous_month_start_dt_aware_for_reviews, previous_month_end_dt_aware_for_reviews]
            ).aggregate(avg=Avg("rating"))["avg"] or 0.0
            previous_avg_rating = round(previous_avg_rating_val, 1)
            rating_change = round(current_avg_rating - previous_avg_rating, 1)
            average_rating = {"value": current_avg_rating, "change": rating_change}
        except Exception as e:
            logger.error(f"Error calculating rating metrics for overview (Business {pk}): {e}", exc_info=True)
            average_rating = {"value": 0, "change": 0}

        # --- "Today's Snapshot" Data ---
        today_snapshot_data = { "today_total_bookings": 0, "today_total_participants": 0, "today_classes_running": 0 }
        try:
            today_instances_qs = ScheduleInstance.objects.filter(
                schedule__option__classId__businessId=business,
                date=today_utc_date, 
                status="scheduled" 
            )
            today_bookings_qs = Booking.objects.filter(
                schedule_instance__in=Subquery(today_instances_qs.values('pk')),
                status="confirmed"
            )
            today_snapshot_data["today_total_bookings"] = today_bookings_qs.count()
            today_snapshot_data["today_total_participants"] = today_bookings_qs.aggregate(
                sum_pax=Coalesce(Sum('participants'), Value(0))
            )['sum_pax']
            today_snapshot_data["today_classes_running"] = today_instances_qs.values(
                'schedule__option__classId'
            ).distinct().count()
        except Exception as e:
            logger.error(f"Error calculating today's snapshot for overview (Business {pk}): {e}", exc_info=True)

        # --- Actionable Prompts Data ---
        actionable_prompts_data = { "new_reviews_count": 0, "stripe_account_status": business.stripe_account_status or "unlinked" }
        try:
            seven_days_ago_aware_dt = timezone.make_aware(datetime.combine(seven_days_ago_utc_date, datetime.min.time()), pytz.utc)
            actionable_prompts_data["new_reviews_count"] = Reviews.objects.filter(
                classId__businessId=business,
                status="approved",
                createdAt__gte=seven_days_ago_aware_dt, 
                business_response__exact='' 
            ).count()
        except Exception as e:
            logger.error(f"Error calculating new reviews count for overview (Business {pk}): {e}", exc_info=True)

        # --- Upcoming Classes ---
        upcoming_classes_data = []
        try:
            upcoming_seven_days_end_date = today_utc_date + timedelta(days=6)
            upcoming_instances_qs = ScheduleInstance.objects.filter(
                schedule__option__classId__businessId=business,
                date__range=[today_utc_date, upcoming_seven_days_end_date],
                status="scheduled"
            ).select_related("schedule__option__classId", "schedule__option").annotate(
                current_participant_spots=Coalesce(Subquery(
                    Booking.objects.filter(schedule_instance=OuterRef("pk"), status="confirmed")
                    .values("schedule_instance").annotate(total_pax=Sum("participants")).values("total_pax")[:1]
                ), Value(0), output_field=IntegerField())
            ).order_by("date", "time")[:5]

            for inst in upcoming_instances_qs:
                naive_schedule_datetime = datetime.combine(inst.date, inst.time)
                display_datetime_str = f"{naive_schedule_datetime.strftime('%b %d')}, {naive_schedule_datetime.strftime('%I:%M %p').lstrip('0') if naive_schedule_datetime.strftime('%I').startswith('0') else naive_schedule_datetime.strftime('%I:%M %p')}"
                class_main_title = inst.schedule.option.classId.title if inst.schedule.option.classId else "Class"
                option_specific_title = inst.schedule.option.title
                display_name = f"{class_main_title} - {option_specific_title}" if option_specific_title and option_specific_title.lower() != class_main_title.lower() else class_main_title
                upcoming_classes_data.append({
                    "name": display_name, "time": display_datetime_str,
                    "current_occupancy": inst.current_participant_spots,
                    "max_occupancy": inst.max_participants,
                })
        except Exception as e:
            logger.error(f"Error getting upcoming classes (Business {pk}): {e}", exc_info=True)
            upcoming_classes_data = []

        # --- Popular Classes ---
        popular_classes_data = []
        try:
            popular_classes_raw = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                status__in=["confirmed", "completed"]
            ).values("schedule_instance__schedule__option__classId__title").annotate(
                enrollment_spots=Sum("participants")
            ).order_by("-enrollment_spots")[:5]
            popular_classes_data = [{"name": entry["schedule_instance__schedule__option__classId__title"], "enrollment": entry["enrollment_spots"] or 0}
                                    for entry in popular_classes_raw if entry["schedule_instance__schedule__option__classId__title"]]
        except Exception as e:
            logger.error(f"Error getting popular classes (Business {pk}): {e}", exc_info=True)
            popular_classes_data = []

        # --- Recent Activity ---
        recent_activity_data = []
        try:
            three_days_ago_utc = now_utc - timedelta(days=3)
            recent_bookings = Booking.objects.filter(
                schedule_instance__schedule__option__classId__businessId=business,
                booking_date__gte=three_days_ago_utc, status="confirmed"
            ).select_related("user").order_by("-booking_date")[:3]
            for booking in recent_bookings:
                recent_activity_data.append({
                    "timestamp": booking.booking_date.isoformat(),
                    "message": (f"New booking: {booking.user.first_name} (+{booking.participants -1} more)" if booking.participants > 1 else f"New booking: {booking.user.first_name}"),
                    "icon": "UserPlus", "color": colors.get("chart", {}).get("blue", "#3b82f6"), "type": "booking"
                })
            recent_payments = Payment.objects.filter(
                booking__schedule_instance__schedule__option__classId__businessId=business,
                status="succeeded", created_at__gte=three_days_ago_utc
            ).order_by("-created_at")[:3]
            for payment in recent_payments:
                recent_activity_data.append({
                    "timestamp": payment.created_at.isoformat(),
                    "message": f"Payment received: ${payment.amount:.2f}",
                    "icon": "DollarSign", "color": colors.get("chart", {}).get("green", "#10b981"), "type": "payment"
                })
            recent_reviews = Reviews.objects.filter(
                classId__businessId=business, status="approved",
                createdAt__gte=three_days_ago_utc
            ).select_related("userId").order_by("-createdAt")[:3]
            for review in recent_reviews:
                recent_activity_data.append({
                    "timestamp": review.createdAt.isoformat(),
                    "message": f"New review: {review.rating}★ from {review.userId.first_name}",
                    "icon": "Star", "color": colors.get("chart", {}).get("orange", "#f97316"), "type": "review"
                })
            recent_activity_data.sort(key=lambda x: x["timestamp"], reverse=True)
            recent_activity_data = recent_activity_data[:5]
        except Exception as e:
            logger.error(f"Error generating recent activity (Business {pk}): {e}", exc_info=True)
            if not recent_activity_data: recent_activity_data = []

        # --- Setup Guide Status ---
        profile_fields_to_check = [
            business.businessDescription,
            business.studentContactEmail,
            business.studentContactPhone,
            business.businessAddress,
            business.businessCity,
            business.businessState,
            business.businessZipCode,
            business.openingTime,
            business.closingTime,
        ]
        is_profile_complete = all(field is not None and str(field).strip() != "" for field in profile_fields_to_check)
        
        # Check for at least one class (any status, as they might be drafting)
        has_created_class = ClassesMain.objects.filter(businessId=business).exists()
        # Check for at least one option linked to any class of this business
        has_class_options = ClassOption.objects.filter(classId__businessId=business).exists()
        # Check for at least one active schedule linked to any option of this business
        has_schedules = Schedule.objects.filter(option__classId__businessId=business, is_active=True).exists()

        setup_progress_data = {
            "is_stripe_connected": business.stripe_account_status == 'active', # Consider 'pending' as partially complete if needed
            "is_profile_complete": is_profile_complete,
            "has_created_class": has_created_class,
            "has_class_options": has_class_options,
            "has_schedules": has_schedules,
            # Add more checks if needed, e.g., first booking received (more complex to track here)
        }

        payload = {
            "metrics": {
                "total_students": total_students, "active_classes": active_classes,
                "monthly_revenue": monthly_revenue, "average_rating": average_rating,
            },
            "today_snapshot": today_snapshot_data,
            "actionable_prompts": actionable_prompts_data,
            "revenue_trend": revenue_trend_data,
            "upcoming_classes": upcoming_classes_data,
            "popular_classes": popular_classes_data,
            "recent_activity": recent_activity_data,
            "setup_progress": setup_progress_data, # Added setup progress
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
    permission_classes = [
        IsAuthenticated,
        CanAccessBusinessDashboard,
    ]  # Checks perm and ownership

    def get_queryset(self):
        """
        Ensures users only see stats for businesses they own or manage.
        """
        user = self.request.user
        return BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).distinct()

    @action(detail=True, methods=["get"])
    def dashboard_stats(self, request, pk=None):
        """Get core stats for the business dashboard."""
        business = self.get_object()  # Applies get_queryset and permission object check
        serializer = self.get_serializer(
            business, context={"request": request}
        )  # Pass instance to serializer
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def revenue_over_time(self, request, pk=None):
        """Get revenue trend data for the business."""
        business = self.get_object()  # Permission/ownership checked
        timeframe = request.query_params.get("timeframe", "monthly")
        # Use business.pk directly for filtering
        bookings_qs = Booking.objects.filter(
            schedule_instance__schedule__option__classId__businessId=business.pk,
            status="completed",
            payment_status="paid",
        )
        if timeframe == "daily":
            days = 30
            start_date = timezone.now().date() - timedelta(
                days=days
            )  # Use date() for filtering GTE date field
            revenue_data = (
                bookings_qs.filter(
                    booking_date__date__gte=start_date  # Filter by date part of booking_date
                )
                .values("booking_date__date")
                .annotate(revenue=Sum("amount_paid"))
                .order_by("booking_date__date")
            )
            response_data = [
                {
                    "date": item["booking_date__date"].isoformat(),
                    "revenue": float(item["revenue"] or 0),
                }
                for item in revenue_data
            ]
        else:  # Monthly
            months = 12
            # Calculate start date more accurately for months
            start_date = (
                timezone.now().date().replace(day=1) - timedelta(days=1)
            ).replace(
                day=1
            )  # Start of previous month
            for _ in range(months - 1):
                start_date = (start_date - timedelta(days=1)).replace(day=1)

            revenue_data = (
                bookings_qs.filter(booking_date__date__gte=start_date)
                .values("booking_date__year", "booking_date__month")
                .annotate(revenue=Sum("amount_paid"))
                .order_by("booking_date__year", "booking_date__month")
            )
            response_data = [
                {
                    "month": f"{item['booking_date__year']}-{item['booking_date__month']:02d}",
                    "revenue": float(item["revenue"] or 0),
                }
                for item in revenue_data
            ]

        return Response(response_data)

    @action(detail=True, methods=["get"])
    def class_performance(self, request, pk=None):
        """Get performance metrics per class for this business."""
        business = self.get_object()  # Permission/ownership checked
        classes_qs = (
            ClassesMain.objects.filter(
                businessId=business  # Use the fetched business object directly
            )
            .annotate(
                booking_count=Count(
                    "options__schedules__instances__bookings",
                    filter=Q(
                        options__schedules__instances__bookings__status="completed",
                        options__schedules__instances__bookings__payment_status="paid",
                    ),
                    distinct=True,
                ),
                revenue=Sum(
                    "options__schedules__instances__bookings__amount_paid",
                    filter=Q(
                        options__schedules__instances__bookings__status="completed",
                        options__schedules__instances__bookings__payment_status="paid",
                    ),
                ),
                review_count=Count(
                    "reviews", filter=Q(reviews__status="approved"), distinct=True
                ),
                average_rating=Avg(
                    "reviews__rating", filter=Q(reviews__status="approved")
                ),
            )
            .values(
                "classId",
                "title",
                "booking_count",
                "revenue",
                "review_count",
                "average_rating",
            )
            .order_by("-revenue")
        )  # Example ordering

        response_data = [
            {
                "classId": item["classId"],
                "title": item["title"],
                "booking_count": item["booking_count"] or 0,
                "revenue": float(item["revenue"] or 0.0),
                "review_count": item["review_count"] or 0,
                "average_rating": round(float(item["average_rating"] or 0.0), 1),
            }
            for item in classes_qs
        ]
        return Response(response_data)


class MyBusinessProfileView(generics.RetrieveUpdateDestroyAPIView):
    """
    Allows authenticated business owners/managers to view, update, or delete
    their OWN associated BusinessInfo profile.
    """
    serializer_class = ManagedBusinessInfoSerializer
    parser_classes = [MultiPartParser, FormParser] # For businessImage upload
    permission_classes = [permissions.IsAuthenticated, CanManageOwnBusinessProfile]

    def get_object(self):
        user = self.request.user
        try:
            business = BusinessInfo.objects.filter(Q(owner=user) | Q(managers=user)).select_related("owner").prefetch_related("managers").first()
            if not business:
                raise NotFound("No business profile associated with this user found.")
            self.check_object_permissions(self.request, business) # Check after fetching
            return business
        except BusinessInfo.DoesNotExist: # Should be caught by first()
            raise NotFound("No business profile associated with this user found.")

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [permissions.IsAuthenticated(), CanDeleteOwnBusinessProfile()]
        return super().get_permissions()

    def perform_update(self, serializer):
        # The ManagedBusinessInfoSerializer's update method handles complex logic.
        # We can still pop fields here if we want to absolutely ensure they are not part of the save call
        # from validated_data, even if the serializer might handle them.
        # This acts as a secondary safeguard at the view level.
        
        # Sensitive fields that should NOT be updatable via this general settings form:
        serializer.validated_data.pop("owner", None) # Owner should not change here
        serializer.validated_data.pop("managers", None) # Manager assignment is a separate process
        serializer.validated_data.pop("verificationStatus", None) # Admin controlled
        serializer.validated_data.pop("stripe_account_id", None) # System controlled
        serializer.validated_data.pop("stripe_account_status", None) # System controlled
        serializer.validated_data.pop("classCategory", None) # Typically set at registration
        # JSON list fields are read-only in serializer, so no need to pop here

        try:
            business = serializer.save() # Serializer's update method is called
            logger.info(
                f"Business Profile '{business.businessName}' (ID: {business.pk}) updated by user {self.request.user.email}"
            )
        except Exception as e:
            logger.error(
                f"Error during MyBusinessProfileView perform_update for Business (User: {self.request.user.email}): {str(e)}",
                exc_info=True,
            )
            # If serializer.save() raises DRFValidationError, it will be handled by DRF.
            # This catches other unexpected errors during save.
            raise DRFValidationError("An error occurred while updating the business profile.")


    def perform_destroy(self, instance): # Keep as is
        business_name = instance.businessName; business_id = instance.businessId
        if instance.businessImage:
            try: instance.businessImage.delete(save=False)
            except Exception as e: logger.warning(f"Could not delete businessImage for {business_id}: {e}")
        # if instance.verificationDocument: # This field was removed from BusinessInfo
        #     try: instance.verificationDocument.delete(save=False)
        #     except Exception as e: logger.warning(f"Could not delete verificationDocument for {business_id}: {e}")
        instance.delete()
        logger.warning(f"Business Profile '{business_name}' (ID: {business_id}) DELETED by owner {self.request.user.email}")