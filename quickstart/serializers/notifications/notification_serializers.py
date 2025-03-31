from rest_framework import serializers
from django.utils import timezone
from django.db.models import Q
from ...models import (
    NotificationCampaign,
    NotificationAttachment, 
    UserSegment, 
    CustomUser
)

class UserSegmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserSegment
        fields = [
            'id', 'name', 'description', 'criteria', 
            'user_count', 'created_at', 'updated_at'
        ]

class NotificationAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    
    class Meta:
        model = NotificationAttachment
        fields = [
            'id', 'name', 'file', 'file_url', 
            'content_type', 'size', 'created_at'
        ]
    
    def get_file_url(self, obj):
        if obj.file:
            return obj.file.url
        return None

class NotificationCampaignSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    attachment_count = serializers.SerializerMethodField()
    segment_name = serializers.SerializerMethodField()
    
    class Meta:
        model = NotificationCampaign
        fields = [
            'id', 'title', 'notification_type', 'subject',
            'content', 'html_content', 'audience_type',
            'segment', 'segment_name', 'status', 'scheduled_for',
            'sent_at', 'recipient_count', 'delivered_count', 
            'success_rate', 'created_by', 'created_by_name', 
            'created_at', 'updated_at', 'attachment_count', 
            'error_message'
        ]
    
    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return "Unknown"
    
    def get_attachment_count(self, obj):
        return obj.attachments.count()
    
    def get_segment_name(self, obj):
        if obj.audience_type == 'segment' and obj.segment:
            try:
                segment = UserSegment.objects.get(id=obj.segment)
                return segment.name
            except UserSegment.DoesNotExist:
                return "Unknown Segment"
        return None

class NotificationCampaignDetailSerializer(NotificationCampaignSerializer):
    attachments = NotificationAttachmentSerializer(many=True, read_only=True)
    
    class Meta(NotificationCampaignSerializer.Meta):
        fields = NotificationCampaignSerializer.Meta.fields + ['attachments']

class NotificationCampaignCreateSerializer(serializers.ModelSerializer):
    individual_users = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        write_only=True
    )
    template_variables = serializers.JSONField(required=False, write_only=True)
    
    class Meta:
        model = NotificationCampaign
        fields = [
            'title', 'notification_type', 'subject',
            'content', 'html_content', 'audience_type',
            'segment', 'status', 'scheduled_for',
            'individual_users', 'template_variables'
        ]
    
    def create(self, validated_data):
        individual_users = validated_data.pop('individual_users', [])
        template_variables = validated_data.pop('template_variables', {})
        
        # Set the created_by field
        validated_data['created_by'] = self.context['request'].user
        
        # Store individual user IDs if targeting individual users
        if validated_data.get('audience_type') == 'individual' and individual_users:
            validated_data['target_user_ids'] = individual_users
            validated_data['recipient_count'] = len(individual_users)
        
        # Set recipient count based on audience type
        if validated_data.get('audience_type') == 'all_users':
            validated_data['recipient_count'] = CustomUser.objects.count()
        elif validated_data.get('audience_type') == 'segment' and validated_data.get('segment'):
            try:
                segment = UserSegment.objects.get(id=validated_data['segment'])
                validated_data['recipient_count'] = segment.user_count
            except UserSegment.DoesNotExist:
                validated_data['recipient_count'] = 0
        
        # Create the campaign
        campaign = NotificationCampaign.objects.create(**validated_data)
        
        return campaign
    
    def update(self, instance, validated_data):
        individual_users = validated_data.pop('individual_users', [])
        template_variables = validated_data.pop('template_variables', {})
        
        # Update basic fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # If status changed to 'sent', update timestamp
        if instance.status == 'sent' and not instance.sent_at:
            instance.sent_at = timezone.now()
        
        # Update target_user_ids if audience type is individual
        if instance.audience_type == 'individual' and individual_users:
            instance.target_user_ids = individual_users
            instance.recipient_count = len(individual_users)
        
        # Update recipient count if audience type changed
        if 'audience_type' in validated_data:
            if instance.audience_type == 'all_users':
                instance.recipient_count = CustomUser.objects.count()
            elif instance.audience_type == 'segment' and instance.segment:
                try:
                    segment = UserSegment.objects.get(id=instance.segment)
                    instance.recipient_count = segment.user_count
                except UserSegment.DoesNotExist:
                    instance.recipient_count = 0
        
        instance.save()
        return instance