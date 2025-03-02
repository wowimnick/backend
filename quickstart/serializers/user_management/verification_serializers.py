from rest_framework import serializers
from django.utils import timezone
from ...models import VerificationRequest, VerificationDocument

class VerificationDocumentSerializer(serializers.ModelSerializer):
    """Serializer for verification documents"""
    document_type_display = serializers.CharField(source='get_document_type_display', read_only=True)
    file_url = serializers.SerializerMethodField()
    
    class Meta:
        model = VerificationDocument
        fields = [
            'id', 'document_type', 'document_type_display', 
            'filename', 'file_type', 'file', 'file_url', 'uploaded_at'
        ]
        read_only_fields = ['id', 'filename', 'file_type', 'uploaded_at']
    
    def get_file_url(self, obj):
        if obj.file:
            return obj.file.url
        return None

class VerificationRequestListSerializer(serializers.ModelSerializer):
    """List serializer for verification requests"""
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email')
    business_name = serializers.CharField(source='business.businessName', read_only=True)
    document_count = serializers.SerializerMethodField()
    role = serializers.CharField(source='user.role.name', read_only=True)
    role_color = serializers.CharField(source='user.role.color', read_only=True)
    user_avatar = serializers.SerializerMethodField()
    business_avatar = serializers.SerializerMethodField()
    reviewer_name = serializers.SerializerMethodField()
    
    class Meta:
        model = VerificationRequest
        fields = [
            'id', 'user', 'user_name', 'user_email', 
            'business', 'business_name', 'status', 
            'submitted_at', 'document_count', 'role',
            'role_color', 'user_avatar', 'business_avatar',
            # Add these fields to make sure they're included in the list response
            'reviewed_by', 'reviewer_name', 'notes', 'rejection_reason',
            'reviewed_at', 'updated_at'
        ]
    
    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()
    
    def get_document_count(self, obj):
        return obj.documents.count()
    
    def get_user_avatar(self, obj):
        if obj.user and obj.user.avatar:
            return obj.user.avatar.url
        return None
    
    def get_business_avatar(self, obj):
        if obj.business and obj.business.businessImage:
            return obj.business.businessImage.url
        return None
        
    def get_reviewer_name(self, obj):
        if obj.reviewed_by:
            return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip()
        return None

class VerificationRequestDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for verification requests"""
    user_name = serializers.SerializerMethodField()
    user_email = serializers.EmailField(source='user.email')
    business_name = serializers.CharField(source='business.businessName', read_only=True)
    documents = VerificationDocumentSerializer(many=True, read_only=True)
    reviewer_name = serializers.SerializerMethodField()
    reviewer_avatar = serializers.SerializerMethodField()
    user_avatar = serializers.SerializerMethodField()
    business_avatar = serializers.SerializerMethodField()
    business_type = serializers.CharField(source='business.businessType', read_only=True)
    business_description = serializers.CharField(source='business.businessDescription', read_only=True)
    role = serializers.CharField(source='user.role.name', read_only=True)
    role_color = serializers.CharField(source='user.role.color', read_only=True)
    
    class Meta:
        model = VerificationRequest
        fields = [
            'id', 'user', 'user_name', 'user_email', 'user_avatar',
            'business', 'business_name', 'business_avatar', 'business_type', 'business_description',
            'status', 'submitted_at', 'updated_at', 'reviewed_by', 'role', 'role_color',
            'reviewer_name', 'reviewer_avatar', 'reviewed_at', 'rejection_reason',
            'notes', 'documents'
        ]
        read_only_fields = [
            'id', 'user', 'business', 'submitted_at', 
            'updated_at', 'reviewer_name', 'reviewer_avatar', 'user_avatar', 'business_avatar',
            'role', 'role_color'
        ]
    
    def get_user_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip()
    
    def get_reviewer_name(self, obj):
        if obj.reviewed_by:
            return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip()
        return None
    
    def get_reviewer_avatar(self, obj):
        if obj.reviewed_by and obj.reviewed_by.avatar:
            return obj.reviewed_by.avatar.url
        return None
    
    def get_user_avatar(self, obj):
        if obj.user and obj.user.avatar:
            return obj.user.avatar.url
        return None
    
    def get_business_avatar(self, obj):
        if obj.business and obj.business.businessImage:
            return obj.business.businessImage.url
        return None
    
class VerificationSubmissionSerializer(serializers.ModelSerializer):
    """Serializer for users submitting verification requests"""
    documents = serializers.ListField(
        child=serializers.FileField(),
        write_only=True
    )
    document_types = serializers.ListField(
        child=serializers.ChoiceField(choices=VerificationDocument.DOCUMENT_TYPES),
        write_only=True
    )
    
    class Meta:
        model = VerificationRequest
        fields = ['business', 'documents', 'document_types', 'notes']
    
    def validate(self, data):
        # Ensure documents and document_types have the same length
        if len(data['documents']) != len(data['document_types']):
            raise serializers.ValidationError(
                "Number of documents and document types must match."
            )
        
        return data
    
    def create(self, validated_data):
        documents = validated_data.pop('documents')
        document_types = validated_data.pop('document_types')
        
        # Create the verification request
        request = VerificationRequest.objects.create(
            user=self.context['request'].user,
            **validated_data
        )
        
        # Create document records
        for i, document_file in enumerate(documents):
            VerificationDocument.objects.create(
                verification_request=request,
                document_type=document_types[i],
                file=document_file,
                filename=document_file.name,
                file_type=document_file.content_type
            )
        
        return request

class VerificationProcessSerializer(serializers.ModelSerializer):
    """Serializer for admins to process verification requests"""
    status = serializers.ChoiceField(choices=VerificationRequest.STATUS_CHOICES)
    
    class Meta:
        model = VerificationRequest
        fields = ['status', 'rejection_reason', 'notes']
    
    def validate(self, data):
        # Map frontend values to backend values if needed
        if 'status' in data:
            if data['status'] == 'approve':
                data['status'] = 'approved'
            elif data['status'] == 'reject':
                data['status'] = 'rejected'
                
        # Ensure rejection reason is provided when rejecting
        if data.get('status') == 'rejected' and not data.get('rejection_reason'):
            raise serializers.ValidationError({
                'rejection_reason': 'Rejection reason is required when rejecting a request.'
            })
        
        return data
    
    def update(self, instance, validated_data):
        # Record who processed the request
        instance.reviewed_by = self.context['request'].user
        instance.reviewed_at = timezone.now()
        
        # Update fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        
        # Update business verification status too
        if instance.business:
            if instance.status == 'approved':
                instance.business.verificationStatus = 'verified'
            elif instance.status == 'rejected':
                instance.business.verificationStatus = 'rejected'
            instance.business.save(update_fields=['verificationStatus'])
        
        return instance