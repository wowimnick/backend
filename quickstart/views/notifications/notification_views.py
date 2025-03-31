# notification_views.py - Updated to reflect the simplified model structure

import time
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.db.models import Q, Count
from ...models import (
    NotificationCampaign,
    NotificationAttachment, 
    UserSegment, 
    CustomUser,
    BusinessInfo,
    Role,
    ClassCategory,
    Booking
)
from ...serializers.notifications.notification_serializers import (
    NotificationCampaignSerializer,
    NotificationCampaignDetailSerializer, 
    NotificationCampaignCreateSerializer,
    NotificationAttachmentSerializer,
    UserSegmentSerializer
)
from ...utils.permissions import IsAdminUser
import resend
from django.conf import settings
import json
import logging
from datetime import timedelta

# Configure Resend
resend.api_key = settings.RESEND_API_KEY
logger = logging.getLogger(__name__)

class AdminNotificationCampaignViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing notification campaigns
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return NotificationCampaignDetailSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return NotificationCampaignCreateSerializer
        return NotificationCampaignSerializer
    
    def get_queryset(self):
        queryset = NotificationCampaign.objects.all()
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter and status_filter != 'all':
            queryset = queryset.filter(status=status_filter)
        
        # Filter by notification type
        notification_type = self.request.query_params.get('notification_type')
        if notification_type and notification_type != 'all':
            queryset = queryset.filter(notification_type=notification_type)
            
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(created_at__range=[start_date, end_date])
        
        # Search
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(subject__icontains=search) |
                Q(content__icontains=search)
            )
            
        return queryset
    
    def perform_create(self, serializer):
        # Save with 'draft' status initially if meant to be sent now
        should_send_now = serializer.validated_data.get('status') == 'sent'
        if should_send_now:
            serializer.validated_data['status'] = 'draft' # Save as draft first

        campaign = serializer.save(created_by=self.request.user)

        if should_send_now:
            # Trigger the send logic immediately after creation
            try:
                logger.info(f"Triggering immediate send for new campaign {campaign.id}")
                # Reuse the send logic but bypass the view action context
                self._process_and_send_campaign(campaign, self.request.data.get('template_variables', {}))
                campaign.status = 'sent' # Update status after sending attempt
                campaign.save(update_fields=['status', 'sent_at', 'recipient_count', 'delivered_count', 'success_rate', 'error_message'])
            except Exception as e:
                logger.error(f"Immediate send failed for campaign {campaign.id}: {str(e)}")
                campaign.status = 'failed'
                campaign.error_message = str(e)
                campaign.save(update_fields=['status', 'error_message'])
                # Optionally raise the error or handle it
                # raise serializers.ValidationError({"detail": f"Failed to send notification: {str(e)}"})

    def perform_update(self, serializer):
        # Save with 'draft' status initially if meant to be sent now
        should_send_now = serializer.validated_data.get('status') == 'sent' and serializer.instance.status != 'sent'

        original_status = serializer.instance.status # Get status before saving
        if should_send_now:
            serializer.validated_data['status'] = 'draft' # Update as draft first

        campaign = serializer.save()

        if should_send_now:
            # Trigger the send logic immediately after update
            try:
                logger.info(f"Triggering immediate send for updated campaign {campaign.id}")
                # Reuse the send logic
                self._process_and_send_campaign(campaign, self.request.data.get('template_variables', {}))
                campaign.status = 'sent' # Update status after sending attempt
                campaign.save(update_fields=['status', 'sent_at', 'recipient_count', 'delivered_count', 'success_rate', 'error_message'])
            except Exception as e:
                logger.error(f"Immediate send failed for campaign {campaign.id}: {str(e)}")
                campaign.status = 'failed'
                campaign.error_message = str(e)
                campaign.save(update_fields=['status', 'error_message'])
                # Optionally raise the error or handle it

    # Add a helper method to encapsulate the sending logic from the 'send' action
    def _process_and_send_campaign(self, campaign, template_variables):
        """Processes recipients and calls the appropriate send method."""
        logger.info(f"Processing campaign {campaign.id} for sending.")

        # Get recipients (reuse logic from the 'send' action)
        recipients = []
        if campaign.audience_type == 'all_users':
            recipients = CustomUser.objects.all()
        elif campaign.audience_type == 'segment' and campaign.segment:
            try:
                segment = UserSegment.objects.get(id=campaign.segment)
                recipients = self._get_segment_users(segment) # Use existing helper
            except UserSegment.DoesNotExist:
                raise ValueError(f"Segment {campaign.segment} not found")
        elif campaign.audience_type == 'individual' and campaign.target_user_ids:
            recipients = CustomUser.objects.filter(userId__in=campaign.target_user_ids)

        campaign.recipient_count = len(recipients)
        campaign.sent_at = timezone.now() # Set send time

        # Call the correct sender based on type
        if campaign.notification_type == 'email':
            self._send_email_notifications(campaign, recipients, template_variables)

        logger.info(f"Finished processing send for campaign {campaign.id}")
        # Note: The campaign status is updated *after* this method returns


    # Keep the existing send action for sending drafts/scheduled later
    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Send a notification campaign (for drafts/scheduled)"""
        campaign = self.get_object()
        logger.info(f"Received request to send campaign {pk} via dedicated /send endpoint.")
        logger.info(f"Request data: {request.data}")

        if campaign.status == 'sent':
            logger.warning(f"Campaign {pk} has already been sent.")
            return Response(
                {'error': 'This campaign has already been sent'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if campaign.status == 'failed':
            logger.warning(f"Attempting to resend failed campaign {pk}.")
            # Allow resending failed campaigns if desired

        template_variables = request.data.get('template_variables', {})

        try:
            # Use the helper method
            self._process_and_send_campaign(campaign, template_variables)

            # Update status after sending
            campaign.status = 'sent'
            campaign.save(update_fields=['status', 'sent_at', 'recipient_count', 'delivered_count', 'success_rate', 'error_message'])
            logger.info(f"Campaign {pk} marked as sent via dedicated endpoint.")

            serializer = self.get_serializer(campaign) # Return updated campaign data
            return Response(serializer.data)

        except Exception as e:
            logger.error(f"Error sending campaign {pk} via dedicated endpoint: {str(e)}", exc_info=True)
            campaign.status = 'failed'
            campaign.error_message = str(e)
            campaign.save(update_fields=['status', 'error_message'])

            return Response(
                {'error': f'Failed to send campaign: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _get_segment_users(self, segment):
        """Get users for a given segment based on segment type"""
        segment_type = segment.name.split(':')[0] if ':' in segment.name else ''
        
        if segment_type == 'role':
            # Get role name from segment name (format: "role:Admin")
            role_name = segment.name.split(':', 1)[1] if ':' in segment.name else ''
            role = Role.objects.filter(name=role_name).first()
            if role:
                return CustomUser.objects.filter(role=role)
        
        elif segment_type == 'category':
            # Get category name from segment name (format: "category:Music")
            category_name = segment.name.split(':', 1)[1] if ':' in segment.name else ''
            category = ClassCategory.objects.filter(name=category_name).first()
            if category:
                # Find users who have bookings in classes of this category
                return CustomUser.objects.filter(
                    bookings__schedule_instance__schedule__option__classId__category=category
                ).distinct()
        
        elif segment_type == 'new_users':
            # Users who joined in the last 30 days
            thirty_days_ago = timezone.now() - timedelta(days=30)
            return CustomUser.objects.filter(createdAt__gte=thirty_days_ago)
        
        elif segment_type == 'inactive_users':
            # Users who haven't booked in the last 60 days but have booked before
            sixty_days_ago = timezone.now() - timedelta(days=60)
            return CustomUser.objects.filter(
                bookings__isnull=False
            ).exclude(
                bookings__booking_date__gte=sixty_days_ago
            ).distinct()
        
        # Default to empty queryset
        return CustomUser.objects.none()
    
    def _send_email_notifications(self, campaign, recipients, template_variables=None):
        """Send email notifications using Resend's batch API"""
        successful_sends = 0
        
        logger.info(f"Sending email notifications for campaign {campaign.id}")
        logger.info(f"Recipients: {len(recipients)}")
        
        # Get email template defaults - fallback if no template variables provided
        template_defaults = getattr(settings, 'EMAIL_TEMPLATE_DEFAULTS', {})
        
        # Use provided template variables or fallback to defaults
        template_vars = template_variables or template_defaults
        logger.info(f"Using template variables: {template_vars}")
        
        # Get attachments
        attachments = []
        for attachment in campaign.attachments.all():
            attachments.append({
                "filename": attachment.name,
                "content": attachment.file.read(),
                "path": attachment.file.name
            })
        logger.info(f"Attachments: {len(attachments)}")
        
        # Get default sending parameters
        notification_settings = getattr(settings, 'NOTIFICATION_SETTINGS', {})
        default_from_email = notification_settings.get('default_from_email', 'notifications@yourdomain.com')
        default_from_name = notification_settings.get('default_from_name', 'Your Company Notifications')
        from_email = f"{default_from_name} <{default_from_email}>"
        logger.info(f"Sending from: {from_email}")
        
        # Process in batches of 100 (Resend's batch API limit)
        batch_size = 100
        
        for i in range(0, len(recipients), batch_size):
            batch = recipients[i:i+batch_size]
            batch_emails = []
            
            logger.info(f"Processing batch {i//batch_size + 1}, size: {len(batch)}")
            
            # Prepare batch of emails
            for recipient in batch:
                try:
                    # Basic email parameters
                    email_params = {
                        "from": from_email,
                        "to": recipient.email,
                        "subject": campaign.subject,
                        "text": campaign.content
                    }
                    
                    # Use HTML content if available
                    if campaign.html_content:
                        # Process HTML template with variables
                        html_content = campaign.html_content
                        
                        # Replace template variables
                        html_content = html_content.replace("{{subject}}", campaign.subject)
                        html_content = html_content.replace("{{content}}", campaign.content)
                        
                        # Replace any other template variables from provided dictionary
                        for key, value in template_vars.items():
                            html_content = html_content.replace(f"{{{{{key}}}}}", str(value))
                        
                        # Set processed HTML as email content
                        email_params["html"] = html_content
                    
                    # Add to batch
                    batch_emails.append(email_params)
                    
                except Exception as e:
                    logger.error(f"Error preparing email for {recipient.email}: {str(e)}")
            
            # Send batch
            try:
                # Verify Resend API key is set
                if not resend.api_key:
                    logger.error("Resend API key is not set!")
                else:
                    logger.info(f"Resend API key is set, first few chars: {resend.api_key[:5]}...")
                
                logger.info(f"Sending batch of {len(batch_emails)} emails via Resend")
                
                # Use the batch send endpoint (instead of individual sends)
                results = resend.Batch.send(batch_emails)
                
                # Count successful sends
                success_count = 0
                for result in results:
                    if result and 'id' in result:
                        successful_sends += 1
                        success_count += 1
                    else:
                        logger.warning(f"Failed to send email, result: {result}")
                
                logger.info(f"Batch sent, successful: {success_count}/{len(batch_emails)}")
                    
            except Exception as e:
                logger.error(f"Error sending batch {i//batch_size + 1}: {str(e)}", exc_info=True)
            
            # Add a delay between batches to avoid hitting rate limits
            if i + batch_size < len(recipients):
                time.sleep(0.5)  # Wait 0.5 second between batches
        
        # Update campaign with delivery stats
        campaign.delivered_count = successful_sends
        if campaign.recipient_count > 0:
            campaign.success_rate = (successful_sends / campaign.recipient_count) * 100
        
        logger.info(f"Email sending complete. Delivered: {successful_sends}/{campaign.recipient_count}, Success rate: {campaign.success_rate}%")
    
    def _send_sms_notifications(self, campaign, recipients):
        """Send SMS notifications"""
        # In a real implementation, this would use Twilio, AWS SNS,
        # or another SMS service
        
        # For this example, we'll simulate 95% successful delivery
        delivered = int(len(recipients) * 0.95)
        campaign.delivered_count = delivered
        campaign.success_rate = (delivered / len(recipients)) * 100 if recipients else 0.0
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a scheduled notification campaign"""
        campaign = self.get_object()
        
        if campaign.status != 'scheduled':
            return Response(
                {'error': 'Only scheduled campaigns can be cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        campaign.status = 'draft'
        campaign.scheduled_for = None
        campaign.save()
        
        return Response({'success': True})
    
    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """Duplicate a notification campaign"""
        campaign = self.get_object()
        
        # Create a new campaign with same data
        new_campaign = NotificationCampaign.objects.create(
            title=f"Copy of {campaign.title}",
            notification_type=campaign.notification_type,
            subject=campaign.subject,
            content=campaign.content,
            html_content=campaign.html_content,
            audience_type=campaign.audience_type,
            segment=campaign.segment,
            target_user_ids=campaign.target_user_ids,
            status='draft',
            recipient_count=campaign.recipient_count,
            created_by=request.user
        )
        
        # Copy attachments if any
        for attachment in campaign.attachments.all():
            NotificationAttachment.objects.create(
                campaign=new_campaign,
                name=attachment.name,
                file=attachment.file,
                content_type=attachment.content_type,
                size=attachment.size
            )
        
        serializer = self.get_serializer(new_campaign)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def metrics(self, request):
        """Get notification metrics for dashboard"""
        total_sent = NotificationCampaign.objects.filter(status='sent').count()
        pending = NotificationCampaign.objects.filter(status='scheduled').count()
        
        by_type = NotificationCampaign.objects.filter(
            status='sent'
        ).values('notification_type').annotate(
            count=Count('id')
        )
        
        by_audience = NotificationCampaign.objects.filter(
            status='sent'
        ).values('audience_type').annotate(
            count=Count('id')
        )
        
        return Response({
            'total_sent': total_sent,
            'pending': pending,
            'by_type': by_type,
            'by_audience': by_audience
        })

class AdminUserSegmentViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing user segments
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = UserSegmentSerializer
    queryset = UserSegment.objects.all()
    
    def list(self, request, *args, **kwargs):
        """Override list to handle hardcoded segments if none exist in DB and refresh counts"""
        queryset = self.get_queryset()
        
        # If no segments exist, create hardcoded segments
        if not queryset.exists():
            self._create_default_segments()
            queryset = self.get_queryset()
        
        # Update counts for all segments
        self._update_segment_counts()
        
        # Re-fetch after update
        queryset = self.get_queryset()
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
    
    def _update_segment_counts(self):
        """Update user counts for all segments"""
        segments = UserSegment.objects.all()
        
        # Make sure we have an all_users segment
        all_users_count = CustomUser.objects.count()
        all_users_segment, created = UserSegment.objects.get_or_create(
            name="all_users",
            defaults={
                'description': "All users in the system",
                'user_count': all_users_count
            }
        )
        
        # If it wasn't just created, update the count
        if not created and all_users_segment.user_count != all_users_count:
            all_users_segment.user_count = all_users_count
            all_users_segment.save(update_fields=['user_count'])
        
        for segment in segments:
            # Get users for this segment
            users = self._get_segment_users(segment)
            count = users.count()
            
            # Only update if count has changed
            if segment.user_count != count:
                segment.user_count = count
                segment.save(update_fields=['user_count'])
        
        return segments
    
    def _create_default_segments(self):
        """Create default hardcoded segments"""
        created_segments = []
        
        # All users segment
        all_users_segment, created = UserSegment.objects.get_or_create(
            name="all_users",
            defaults={
                'description': "All users in the system",
                'user_count': CustomUser.objects.count()
            }
        )
        if created:
            created_segments.append(all_users_segment)
        
        # Role-based segments
        for role in Role.objects.all():
            segment, created = UserSegment.objects.get_or_create(
                name=f"role:{role.name}",
                defaults={
                    'description': f"All users with the {role.name} role",
                    'criteria': {'role': role.pk},
                    'user_count': CustomUser.objects.filter(role=role).count()
                }
            )
            if created:
                created_segments.append(segment)
        
        # Category-based segments
        for category in ClassCategory.objects.all():
            segment, created = UserSegment.objects.get_or_create(
                name=f"category:{category.name}",
                defaults={
                    'description': f"Users who have booked classes in the {category.name} category",
                    'criteria': {'class_category': category.pk},
                    'user_count': CustomUser.objects.filter(
                        bookings__schedule_instance__schedule__option__classId__category=category
                    ).distinct().count()
                }
            )
            if created:
                created_segments.append(segment)
        
        # Special segments
        # New users (last 30 days)
        thirty_days_ago = timezone.now() - timedelta(days=30)
        segment, created = UserSegment.objects.get_or_create(
            name="new_users",
            defaults={
                'description': "Users who joined in the last 30 days",
                'criteria': {'joined_after': thirty_days_ago.isoformat()},
                'user_count': CustomUser.objects.filter(createdAt__gte=thirty_days_ago).count()
            }
        )
        if created:
            created_segments.append(segment)
        
        # Inactive users (no bookings in 60 days)
        sixty_days_ago = timezone.now() - timedelta(days=60)
        segment, created = UserSegment.objects.get_or_create(
            name="inactive_users",
            defaults={
                'description': "Users who haven't booked a class in the last 60 days",
                'criteria': {'no_bookings_since': sixty_days_ago.isoformat()},
                'user_count': CustomUser.objects.filter(
                    bookings__isnull=False
                ).exclude(
                    bookings__booking_date__gte=sixty_days_ago
                ).distinct().count()
            }
        )
        if created:
            created_segments.append(segment)
        
        return created_segments
    
    @action(detail=True, methods=['get'])
    def users(self, request, pk=None):
        """Get users in this segment"""
        segment = self.get_object()
        segment_type = segment.name.split(':')[0] if ':' in segment.name else segment.name
        
        # Get users based on segment type
        users = self._get_segment_users(segment)
        
        return Response({
            'count': users.count(),
            'users': [
                {
                    'id': user.userId,
                    'email': user.email,
                    'name': user.get_full_name() or user.username
                }
                for user in users[:100]  # Limit to 100 users in response
            ]
        })
    
    def _get_segment_users(self, segment):
        """Get users for a given segment based on segment name or criteria"""
        segment_name = segment.name
        
        # Handle special segment names
        if segment_name == 'all_users':
            return CustomUser.objects.all()
        
        # Handle segmentation by prefix (e.g., role:Admin, category:Music)
        if ':' in segment_name:
            segment_type, segment_value = segment_name.split(':', 1)
            
            if segment_type == 'role':
                role = Role.objects.filter(name=segment_value).first()
                if role:
                    return CustomUser.objects.filter(role=role)
            
            elif segment_type == 'category':
                category = ClassCategory.objects.filter(name=segment_value).first()
                if category:
                    return CustomUser.objects.filter(
                        bookings__schedule_instance__schedule__option__classId__category=category
                    ).distinct()
        
        # Handle special segment types
        if segment_name == 'new_users':
            thirty_days_ago = timezone.now() - timedelta(days=30)
            return CustomUser.objects.filter(createdAt__gte=thirty_days_ago)
        
        elif segment_name == 'inactive_users':
            sixty_days_ago = timezone.now() - timedelta(days=60)
            return CustomUser.objects.filter(
                bookings__isnull=False
            ).exclude(
                bookings__booking_date__gte=sixty_days_ago
            ).distinct()
        
        # For custom segments with criteria
        if segment.criteria:
            queryset = CustomUser.objects.all()
            
            if 'role' in segment.criteria:
                queryset = queryset.filter(role_id=segment.criteria['role'])
            
            if 'joined_after' in segment.criteria:
                try:
                    date = timezone.datetime.fromisoformat(segment.criteria['joined_after'])
                    queryset = queryset.filter(createdAt__gte=date)
                except (ValueError, TypeError):
                    pass
            
            if 'no_bookings_since' in segment.criteria:
                try:
                    date = timezone.datetime.fromisoformat(segment.criteria['no_bookings_since'])
                    queryset = queryset.filter(
                        bookings__isnull=False
                    ).exclude(
                        bookings__booking_date__gte=date
                    ).distinct()
                except (ValueError, TypeError):
                    pass
            
            return queryset
        
        # Default to empty queryset
        return CustomUser.objects.none()
    
    def create(self, request, *args, **kwargs):
        """Block creation of segments from API"""
        return Response({
            'error': 'User segments cannot be created via the API'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    def update(self, request, *args, **kwargs):
        """Block updating of segments from API"""
        return Response({
            'error': 'User segments cannot be updated via the API'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    def destroy(self, request, *args, **kwargs):
        """Block deletion of segments from API"""
        return Response({
            'error': 'User segments cannot be deleted via the API'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

class AdminNotificationAttachmentViewSet(viewsets.ModelViewSet):
    """
    Admin viewset for managing notification attachments
    """
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = NotificationAttachmentSerializer
    queryset = NotificationAttachment.objects.all()
    
    def perform_create(self, serializer):
        # Set file size based on actual file
        file_obj = self.request.FILES.get('file')
        if file_obj:
            serializer.save(size=file_obj.size)
        else:
            serializer.save()