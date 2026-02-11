import time
from rest_framework import viewsets, status, filters  # Added filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db import transaction  # Added transaction
from django.db.models import Q, Count, Sum, Avg, F, Value
from django.db.models.functions import Coalesce  # Added Coalesce
from rest_framework.pagination import PageNumberPagination
from quickstart.utils.permissions import (
    IsAuthenticated,
    BasePermission,
    CanAccessNotificationAdmin,
    CanAccessSegmentAdmin,
)
from quickstart.models import (
    NotificationCampaign,
    NotificationAttachment,
    UserSegment,
    CustomUser,
    BusinessInfo,
    Role,
    ClassCategory,
    Booking,
)
from quickstart.serializers.admin.notifications.notification_serializers import (
    NotificationCampaignSerializer,
    NotificationCampaignDetailSerializer,
    NotificationCampaignCreateSerializer,
    NotificationAttachmentSerializer,
    UserSegmentSerializer,
)
import resend  # Ensure resend is imported
from django.conf import settings
import logging
from datetime import timedelta

# Configure Resend
resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger(__name__)


# --- AdminNotificationCampaignViewSet ---


class AdminNotificationCampaignViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing notification campaigns
    """

    permission_classes = [IsAuthenticated, CanAccessNotificationAdmin]
    queryset = (
        NotificationCampaign.objects.select_related("created_by")
        .prefetch_related("attachments")
        .all()
    )  # Optimize base queryset
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "subject", "content", "created_by__email"]
    ordering_fields = [
        "title",
        "created_at",
        "scheduled_for",
        "sent_at",
        "status",
        "notification_type",
    ]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return NotificationCampaignDetailSerializer
        elif self.action in ["create", "update", "partial_update"]:
            return NotificationCampaignCreateSerializer
        return NotificationCampaignSerializer  # Default for list

    def get_queryset(self):
        # Apply base permission check first
        if not self.request.user.has_perm("quickstart.view_notificationcampaign"):
            logger.warning(
                f"User {self.request.user.email} denied access to list campaigns (missing view_notificationcampaign perm)."
            )
            return NotificationCampaign.objects.none()

        queryset = super().get_queryset()  # Get queryset from class attribute

        # --- Filtering Logic ---
        status_filter = self.request.query_params.get("status")
        if status_filter and status_filter != "all":
            # Ensure valid status
            valid_statuses = [
                choice[0] for choice in NotificationCampaign.STATUS_CHOICES
            ]
            if status_filter in valid_statuses:
                queryset = queryset.filter(status=status_filter)

        notification_type = self.request.query_params.get("notification_type")
        if notification_type and notification_type != "all":
            # Ensure valid type
            valid_types = [
                choice[0] for choice in NotificationCampaign.NOTIFICATION_TYPES
            ]
            if notification_type in valid_types:
                queryset = queryset.filter(notification_type=notification_type)

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date and end_date:
            try:
                start_dt = timezone.datetime.strptime(start_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                end_dt = timezone.datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
                # Filter based on created_at or maybe sent_at/scheduled_for depending on context?
                queryset = queryset.filter(created_at__range=[start_dt, end_dt])
            except ValueError:
                logger.warning(
                    f"Invalid date format for campaign filter: start={start_date}, end={end_date}"
                )

        return queryset

    # --- Standard Action Overrides with Permissions ---

    def create(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.add_notificationcampaign"):
            self.permission_denied(
                request, message="You do not have permission to create campaigns."
            )

        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        should_send_now = serializer.validated_data.get("status") == "sent"
        if should_send_now:
            if not request.user.has_perm("quickstart.send_notification_campaign"):
                self.permission_denied(
                    request,
                    message="You have permission to create, but not to send campaigns immediately.",
                )
            serializer.validated_data["status"] = "draft"  # Save as draft first

        self.perform_create(serializer)
        campaign = serializer.instance

        if should_send_now:
            template_variables = request.data.get("template_variables", {})
            task_result = send_campaign_task.delay(
                campaign_id=campaign.id, template_variables=template_variables
            )
            campaign.status = "sending"
            campaign.celery_task_id = task_result.id
            campaign.save(update_fields=["status", "celery_task_id"])
            logger.info(
                f"New campaign {campaign.id} immediately dispatched to Celery with task ID {task_result.id}."
            )

        detail_serializer = NotificationCampaignDetailSerializer(
            campaign, context={"request": request}
        )
        headers = self.get_success_headers(detail_serializer.data)
        return Response(
            detail_serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def perform_create(self, serializer):
        instance = serializer.save(created_by=self.request.user)
        logger.info(
            f"Campaign '{instance.title}' created by Admin {self.request.user.email}"
        )

    def update(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.change_notificationcampaign"):
            self.permission_denied(
                request, message="You do not have permission to update campaigns."
            )

        partial = kwargs.pop("partial", True)  # Default to partial update (PATCH)
        instance = self.get_object()

        # Prevent updates to campaigns that are already in a final or active state
        if instance.status in ["sending", "sent"]:
            return Response(
                {
                    "detail": f"Cannot update a campaign with status '{instance.status}'. Please duplicate it instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)

        self.perform_update(serializer)

        if getattr(instance, "_prefetched_objects_cache", None):
            # If 'prefetch_related' has been used, we need to clear the cache
            # to prevent stale data from being returned.
            instance._prefetched_objects_cache = {}

        # Return the updated data using the detailed serializer
        detail_serializer = NotificationCampaignDetailSerializer(
            instance, context={"request": request}
        )
        return Response(detail_serializer.data)

    def perform_update(self, serializer):
        # Saves instance
        instance = serializer.save()
        logger.info(
            f"Campaign '{instance.title}' (ID: {instance.pk}) updated by Admin {self.request.user.email}"
        )

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_notificationcampaign"):
            self.permission_denied(
                request, message="You do not have permission to delete campaigns."
            )
        instance = self.get_object()
        if instance.status == "sent":
            return Response({"detail": "Cannot delete a sent campaign."}, status=400)
        logger.warning(
            f"Campaign '{instance.title}' (ID: {instance.pk}) deleted by Admin {request.user.email}"
        )
        return super().destroy(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_notificationcampaign"):
            self.permission_denied(request, message="You cannot view campaign details.")
        return super().retrieve(request, *args, **kwargs)

    # --- Custom Actions with Permissions ---

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        """Send a notification campaign by dispatching a Celery task."""
        if not request.user.has_perm("quickstart.send_notification_campaign"):
            self.permission_denied(
                request, message="You do not have permission to send campaigns."
            )

        campaign = self.get_object()
        logger.info(f"Admin {request.user.email} initiated send for campaign {pk}.")

        if campaign.status in ["sending", "sent"]:
            logger.warning(
                f"Attempt to send campaign {pk} which is already '{campaign.status}'."
            )
            return Response(
                {"error": f"This campaign is already {campaign.status}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        template_variables = request.data.get("template_variables", {})

        try:
            # Dispatch the master task to Celery
            task_result = send_campaign_task.delay(
                campaign_id=campaign.id, template_variables=template_variables
            )

            # Update the campaign to reflect it's now being sent
            campaign.status = "sending"
            campaign.celery_task_id = task_result.id
            campaign.save(update_fields=["status", "celery_task_id"])

            logger.info(
                f"Campaign {pk} dispatched to Celery with task ID {task_result.id}."
            )

            serializer = self.get_serializer(campaign)
            return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

        except Exception as e:
            logger.error(
                f"Error dispatching Celery task for campaign {pk}: {str(e)}",
                exc_info=True,
            )
            campaign.status = "failed"
            campaign.error_message = (
                f"Failed to dispatch to background worker: {str(e)}"
            )
            campaign.save(update_fields=["status", "error_message"])
            return Response(
                {"error": "Failed to start the sending process."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=True, methods=["get"], url_path="send-progress")
    def get_send_progress(self, request, pk=None):
        """
        Pollable endpoint for the frontend to get the sending progress of a campaign.
        """
        campaign = self.get_object()
        cache_key = get_progress_cache_key(campaign.id)
        progress_data = cache.get(cache_key)

        if progress_data:
            return Response(progress_data)

        # If no data in cache, provide a default based on DB status
        if campaign.status == "sending":
            return Response(
                {
                    "status": "sending",
                    "processed": 0,
                    "total": campaign.recipient_count or 0,
                    "message": "Initializing...",
                }
            )
        elif campaign.status == "sent":
            return Response(
                {
                    "status": "complete",
                    "processed": campaign.recipient_count,
                    "total": campaign.recipient_count,
                }
            )
        elif campaign.status == "failed":
            return Response(
                {
                    "status": "error",
                    "message": campaign.error_message or "An unknown error occurred.",
                }
            )
        else:
            return Response({"status": "pending", "message": "Not yet sent."})

    # Internal helper - No permission check needed
    def _get_segment_users_by_id(self, segment_id):
        """Helper to get UserSegment instance and call _get_segment_users"""
        try:
            segment = UserSegment.objects.get(id=segment_id)
            return self._get_segment_users(segment)
        except (UserSegment.DoesNotExist, ValueError, TypeError):
            logger.error(f"Segment ID {segment_id} not found or invalid.")
            return CustomUser.objects.none()

    # Internal helper - No permission check needed
    def _get_segment_users(self, segment):
        """Get users for a given segment object based on segment name or criteria"""
        segment_name = segment.name

        if segment_name == "all_users":
            return CustomUser.objects.all()

        # Handle segmentation by prefix (e.g., role:Admin, category:Music)
        if ":" in segment_name:
            segment_type, segment_value = segment_name.split(":", 1)

            if segment_type == "role":
                role = Role.objects.filter(name=segment_value).first()
                if role:
                    return CustomUser.objects.filter(role=role)
                else:
                    logger.warning(f"Segment role '{segment_value}' not found.")
                    return CustomUser.objects.none()

            elif segment_type == "category":
                category = ClassCategory.objects.filter(name=segment_value).first()
                if category:
                    # Find users who have bookings in classes of this category
                    return CustomUser.objects.filter(
                        bookings__schedule_instance__schedule__option__classId__category=category
                    ).distinct()
                else:
                    logger.warning(f"Segment category '{segment_value}' not found.")
                    return CustomUser.objects.none()

        # Handle special segment types based on name convention
        if segment_name == "new_users":
            thirty_days_ago = timezone.now() - timedelta(days=30)
            return CustomUser.objects.filter(createdAt__gte=thirty_days_ago)

        elif segment_name == "inactive_users":
            # Users who have booked, but not in the last 60 days
            sixty_days_ago = timezone.now() - timedelta(days=60)
            # Get IDs of users who HAVE booked recently
            active_booking_user_ids = (
                Booking.objects.filter(booking_date__gte=sixty_days_ago)
                .values_list("user_id", flat=True)
                .distinct()
            )
            # Return users who have booked ever, but are not in the recent list
            return (
                CustomUser.objects.filter(
                    bookings__isnull=False  # Has booked at least once
                )
                .exclude(
                    id__in=active_booking_user_ids  # Exclude those who booked recently
                )
                .distinct()
            )

        # For custom segments with criteria (if you implement this later)
        if isinstance(segment.criteria, dict) and segment.criteria:
            queryset = CustomUser.objects.all()
            logger.info(
                f"Applying custom criteria for segment '{segment_name}': {segment.criteria}"
            )

            # Example criteria handling (expand as needed)
            if "role" in segment.criteria:
                role_id = segment.criteria["role"]
                if isinstance(role_id, int):
                    queryset = queryset.filter(role_id=role_id)
                elif isinstance(role_id, str):  # Allow role name maybe? Less robust.
                    queryset = queryset.filter(role__name=role_id)

            if "joined_after" in segment.criteria:
                try:
                    date_str = segment.criteria["joined_after"]
                    # Attempt to parse ISO format or YYYY-MM-DD
                    if "T" in date_str:
                        date = timezone.datetime.fromisoformat(date_str)
                    else:
                        date = timezone.datetime.strptime(date_str, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                    queryset = queryset.filter(createdAt__gte=date)
                except (ValueError, TypeError):
                    logger.warning(
                        f"Invalid date format in segment criteria 'joined_after': {segment.criteria['joined_after']}"
                    )
                    pass

            return queryset.distinct()

        # Default to empty queryset if no matching logic found
        logger.warning(
            f"Could not determine user set for segment '{segment_name}' (ID: {segment.id})"
        )
        return CustomUser.objects.none()

    # Internal helper - No permission check needed
    def _send_sms_notifications(self, campaign, recipients_info):
        """Send SMS notifications (Placeholder)"""
        total_recipients = len(recipients_info)
        logger.info(
            f"Simulating SMS send for campaign {campaign.id} to {total_recipients} recipients."
        )
        # --- Placeholder Logic ---
        delivered_count = int(total_recipients * 0.95)  # Simulate 95% success
        campaign.delivered_count = delivered_count
        campaign.success_rate = (
            (delivered_count / total_recipients * 100) if total_recipients > 0 else 0.0
        )
        logger.info(
            f"SMS simulation complete. Delivered: {delivered_count}/{total_recipients}"
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a scheduled notification campaign"""
        if not request.user.has_perm("quickstart.cancel_notification_campaign"):
            self.permission_denied(request, message="You cannot cancel campaigns.")

        campaign = self.get_object()
        if campaign.status != "scheduled":
            return Response(
                {"error": "Only scheduled campaigns can be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        campaign.status = "draft"
        campaign.scheduled_for = None
        campaign.save(update_fields=["status", "scheduled_for"])
        logger.info(
            f"Campaign '{campaign.title}' (ID: {pk}) cancelled by Admin {request.user.email}"
        )
        # Add to AuditLog if needed

        return Response({"success": True, "status": "draft"})

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """Duplicate a notification campaign"""
        if not request.user.has_perm("quickstart.duplicate_notification_campaign"):
            self.permission_denied(request, message="You cannot duplicate campaigns.")

        campaign = self.get_object()

        try:
            with transaction.atomic():  # Ensure atomicity
                # Create shallow copy
                new_campaign = NotificationCampaign.objects.get(pk=pk)
                new_campaign.pk = None
                new_campaign.id = None  # Reset UUID if it's PK
                new_campaign.title = f"Copy of {campaign.title}"[
                    :255
                ]  # Ensure title length
                new_campaign.status = "draft"
                new_campaign.scheduled_for = None
                new_campaign.sent_at = None
                new_campaign.delivered_count = 0
                new_campaign.success_rate = 0.0
                new_campaign.error_message = None
                new_campaign.created_by = request.user  # Assign current user
                # created_at/updated_at set on save
                new_campaign.save()

                # Copy attachments
                attachments_to_create = []
                for attachment in campaign.attachments.all():
                    # Create new attachment instance, handle file copy correctly
                    # Depending on storage, just assigning attachment.file might work,
                    # but safer might be to read content and save to a new file if needed.
                    # For S3, assigning the file path might be sufficient if files are unique.
                    attachments_to_create.append(
                        NotificationAttachment(
                            campaign=new_campaign,
                            name=attachment.name,
                            file=attachment.file,  # Assuming this references the storage path
                            content_type=attachment.content_type,
                            size=attachment.size,
                        )
                    )
                if attachments_to_create:
                    NotificationAttachment.objects.bulk_create(attachments_to_create)

            logger.info(
                f"Campaign '{campaign.title}' (ID: {pk}) duplicated to new campaign (ID: {new_campaign.pk}) by Admin {request.user.email}"
            )
            serializer = NotificationCampaignDetailSerializer(
                new_campaign, context={"request": request}
            )
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.error(f"Error duplicating campaign {pk}: {str(e)}", exc_info=True)
            return Response(
                {"error": "Failed to duplicate campaign."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    @action(detail=False, methods=["get"])
    def metrics(self, request):
        """Get notification metrics for dashboard"""
        if not request.user.has_perm("quickstart.view_notification_metrics"):
            self.permission_denied(
                request, message="You cannot view notification metrics."
            )

        # Aggregate counts
        counts = NotificationCampaign.objects.aggregate(
            total_sent=Count("id", filter=Q(status="sent")),
            pending=Count("id", filter=Q(status="scheduled")),
            drafts=Count("id", filter=Q(status="draft")),
            failed=Count("id", filter=Q(status="failed")),
            # Calculate average success rate across sent campaigns
            average_success_rate=Coalesce(
                Avg("success_rate", filter=Q(status="sent")), Value(0.0)
            ),
        )

        # Group by type/audience
        by_type = list(
            NotificationCampaign.objects.values("notification_type")
            .annotate(count=Count("id"))
            .order_by()
        )
        by_audience = list(
            NotificationCampaign.objects.values("audience_type")
            .annotate(count=Count("id"))
            .order_by()
        )

        # Success rate trend (fetch more details if needed)
        recent_campaigns = NotificationCampaign.objects.filter(status="sent").order_by(
            "-sent_at"
        )[:5]
        success_trend = [
            {"title": c.title, "rate": round(c.success_rate, 1), "sent_at": c.sent_at}
            for c in recent_campaigns
        ]

        return Response(
            {
                "total_sent": counts["total_sent"],
                "pending_notifications": counts["pending"],  # Renamed key for clarity
                "drafts": counts["drafts"],
                "failed": counts["failed"],
                "average_success_rate": counts[
                    "average_success_rate"
                ],  # Added avg rate
                "by_type": by_type,
                "by_audience": by_audience,
                "success_trend": success_trend,
            }
        )


# --- AdminUserSegmentViewSet ---


class SimplePageNumberPagination(PageNumberPagination):
    page_size = 100  # Show more users per page for segment view?
    page_size_query_param = "page_size"
    max_page_size = 200


class AdminUserSegmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin viewset for managing user segments (primarily read-only via API)
    """

    permission_classes = [IsAuthenticated, CanAccessSegmentAdmin]
    serializer_class = UserSegmentSerializer
    queryset = UserSegment.objects.order_by("name")
    http_method_names = ["get", "post", "head", "options"]
    pagination_class = SimplePageNumberPagination

    # Reuse helper from campaign viewset
    _get_segment_users = AdminNotificationCampaignViewSet._get_segment_users

    def list(self, request, *args, **kwargs):
        """Override list to handle hardcoded segments if none exist in DB and refresh counts"""
        if not request.user.has_perm("quickstart.view_usersegment"):
            self.permission_denied(request, message="You cannot view user segments.")

        # --- Auto-create/refresh logic ---
        force_refresh = request.query_params.get("refresh_counts") == "true"
        needs_initial_setup = not UserSegment.objects.exists()

        if force_refresh:
            if not request.user.has_perm("quickstart.refresh_segment_counts"):
                self.permission_denied(
                    request,
                    message="You do not have permission to refresh segment counts.",
                )
            logger.info(f"User {request.user.email} triggered segment count refresh.")
            try:
                self._update_segment_counts()
                if needs_initial_setup:
                    self._create_default_segments()
            except Exception as e:
                # Log error but potentially still return existing segments
                logger.error(
                    f"Error during segment count refresh/setup: {e}", exc_info=True
                )
        elif needs_initial_setup:
            logger.info("No segments found, creating default segments.")
            try:
                self._create_default_segments()
                self._update_segment_counts()
            except Exception as e:
                logger.error(f"Error during initial segment setup: {e}", exc_info=True)

        # Re-fetch queryset after potential updates/creation
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_usersegment"):
            self.permission_denied(request, message="You cannot view segment details.")
        return super().retrieve(request, *args, **kwargs)

    # Internal helper, no permission needed here
    def _update_segment_counts(self):
        """Update user counts for all segments"""
        logger.info("Starting segment count update process.")
        all_users_count = CustomUser.objects.filter(is_active=True).count()
        try:
            all_users_segment, created = UserSegment.objects.get_or_create(
                name="all_users",
                defaults={
                    "description": "All active users",
                    "user_count": all_users_count,
                },
            )
            if not created and all_users_segment.user_count != all_users_count:
                all_users_segment.user_count = all_users_count
                all_users_segment.save(update_fields=["user_count"])

            segments_to_update = []
            for segment in UserSegment.objects.exclude(name="all_users"):
                try:
                    # Ensure we count only active users for consistency maybe?
                    current_count = (
                        self._get_segment_users(segment).filter(is_active=True).count()
                    )
                    if segment.user_count != current_count:
                        segment.user_count = current_count
                        segments_to_update.append(segment)
                except Exception as e:
                    logger.error(
                        f"Error calculating count for segment '{segment.name}': {e}"
                    )

            if segments_to_update:
                UserSegment.objects.bulk_update(segments_to_update, ["user_count"])
                logger.info(f"Updated counts for {len(segments_to_update)} segments.")

        except Exception as e:
            logger.error(f"Error during segment count update: {e}", exc_info=True)
            raise  # Re-raise to indicate failure in refresh action if called

    # Internal helper, no permission needed here
    def _create_default_segments(self):
        """Create default hardcoded segments (Idempotent using get_or_create)"""
        logger.info("Creating default segments...")
        created_segments_count = 0

        # All users segment
        _, created = UserSegment.objects.get_or_create(
            name="all_users", defaults={"description": "All active users"}
        )
        if created:
            created_segments_count += 1

        # Role-based segments
        for role in Role.objects.all():
            _, created = UserSegment.objects.get_or_create(
                name=f"role:{role.name}",
                defaults={"description": f"All users with the {role.name} role"},
            )
            if created:
                created_segments_count += 1

        # Category-based segments
        for category in ClassCategory.objects.all():
            _, created = UserSegment.objects.get_or_create(
                name=f"category:{category.name}",
                defaults={
                    "description": f"Users who have booked classes in the {category.name} category"
                },
            )
            if created:
                created_segments_count += 1

        # Special segments (New users)
        _, created = UserSegment.objects.get_or_create(
            name="new_users",
            defaults={"description": "Users who joined in the last 30 days"},
        )
        if created:
            created_segments_count += 1

        # Special segments (Inactive users)
        _, created = UserSegment.objects.get_or_create(
            name="inactive_users",
            defaults={
                "description": "Users who haven't booked a class in the last 60 days"
            },
        )
        if created:
            created_segments_count += 1

        logger.info(
            f"Default segment creation process finished. Created {created_segments_count} new segments."
        )

    @action(detail=True, methods=["get"])
    def users(self, request, pk=None):
        """Get users in this segment"""
        if not request.user.has_perm("quickstart.view_segment_users"):
            self.permission_denied(
                request, message="You cannot view users within segments."
            )

        segment = self.get_object()
        # Select only necessary fields for performance
        users_qs = (
            self._get_segment_users(segment)
            .filter(is_active=True)
            .only(
                "userId",
                "email",
                "first_name",
                "last_name",
                "avatar",  # Add avatar if needed
            )
            .order_by("last_name", "first_name")
        )  # Add ordering

        paginator = self.pagination_class()
        paginated_users = paginator.paginate_queryset(users_qs, request, view=self)

        user_data = []
        if paginated_users is not None:
            for user in paginated_users:
                avatar_url = None
                if hasattr(user, "get_avatar_url"):
                    avatar_url = user.get_avatar_url()
                elif user.avatar and hasattr(user.avatar, "url"):
                    avatar_url = user.avatar.url

                user_data.append(
                    {
                        "id": user.userId,
                        "email": user.email,
                        "name": user.get_full_name() or user.email,
                        "avatar_url": avatar_url,  # Include avatar URL
                    }
                )
        else:  # Fallback if pagination is somehow disabled
            user_data = [
                {
                    "id": user.userId,
                    "email": user.email,
                    "name": user.get_full_name() or user.email,
                    "avatar_url": (
                        user.get_avatar_url()
                        if hasattr(user, "get_avatar_url")
                        else (user.avatar.url if user.avatar else None)
                    ),
                }
                for user in users_qs  # Iterate directly over the queryset
            ]

        if paginated_users is not None:
            # Use paginator's method to get the response with headers/count
            return paginator.get_paginated_response(user_data)

        # Fallback if no pagination class was somehow assigned
        # Provide data structure consistent with pagination response
        return Response(
            {
                "count": users_qs.count(),
                "next": None,
                "previous": None,
                "results": user_data,
            }
        )

    # Add action to manually trigger refresh
    @action(detail=False, methods=["post"], url_path="refresh-counts")
    def refresh_counts(self, request):
        """Manually trigger the segment count refresh."""
        if not request.user.has_perm("quickstart.refresh_segment_counts"):
            self.permission_denied(
                request, message="You do not have permission to refresh segment counts."
            )
        try:
            self._update_segment_counts()
            return Response(
                {"status": "success", "message": "Segment counts refreshed."}
            )
        except Exception as e:
            logger.error(f"Manual segment count refresh failed: {e}", exc_info=True)
            return Response(
                {"status": "error", "message": "Failed to refresh segment counts."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# --- AdminNotificationAttachmentViewSet ---


class AdminNotificationAttachmentViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing notification attachments
    """

    permission_classes = [
        IsAuthenticated,
        CanAccessNotificationAdmin,
    ]  # Use campaign admin access
    serializer_class = NotificationAttachmentSerializer
    queryset = NotificationAttachment.objects.select_related(
        "campaign"
    ).all()  # Add campaign relation
    http_method_names = [
        "get",
        "post",
        "delete",
        "head",
        "options",
    ]  # Allow POST, GET, DELETE

    # --- Standard Methods with Permissions ---
    def create(self, request, *args, **kwargs):
        # Check permission to add attachments
        if not request.user.has_perm("quickstart.add_notificationattachment"):
            self.permission_denied(request, message="You cannot upload attachments.")

        # Check permission to modify the target campaign
        campaign_id = request.data.get("campaign")
        if not campaign_id:
            return Response(
                {"campaign": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            campaign = NotificationCampaign.objects.get(pk=campaign_id)
            if not request.user.has_perm("quickstart.change_notificationcampaign"):
                # If user needs change perm on campaign to add attachments
                self.permission_denied(
                    request,
                    message=f"You do not have permission to modify campaign {campaign_id}.",
                )
            if campaign.status == "sent":
                return Response(
                    {"detail": "Cannot add attachments to a sent campaign."}, status=400
                )
        except NotificationCampaign.DoesNotExist:
            return Response(
                {"campaign": ["Campaign not found."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        file_obj = self.request.FILES.get("file")
        if file_obj:
            # Save with size and log
            instance = serializer.save(size=file_obj.size)
            logger.info(
                f"Attachment '{file_obj.name}' (ID: {instance.pk}) uploaded by Admin {self.request.user.email} for Campaign {instance.campaign_id}"
            )
        else:
            # Should be caught by serializer validation if file is required
            serializer.save()

    def destroy(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.delete_notificationattachment"):
            self.permission_denied(request, message="You cannot delete attachments.")
        instance = self.get_object()
        if instance.campaign.status == "sent":
            return Response(
                {"detail": "Cannot delete attachments from a sent campaign."},
                status=400,
            )
        logger.warning(
            f"Attachment '{instance.name}' (ID: {instance.pk}) deleted by Admin {request.user.email} from Campaign {instance.campaign_id}"
        )
        return super().destroy(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_notificationattachment"):
            self.permission_denied(request, message="You cannot view attachments.")
        # Filter by campaign_id if provided
        campaign_id = request.query_params.get("campaign_id")
        if campaign_id:
            # Apply filtering to the base queryset
            queryset = self.filter_queryset(
                self.get_queryset().filter(campaign_id=campaign_id)
            )
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        # If no campaign filter, proceed with default list (might list all attachments)
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        if not request.user.has_perm("quickstart.view_notificationattachment"):
            self.permission_denied(
                request, message="You cannot view attachment details."
            )
        return super().retrieve(request, *args, **kwargs)
