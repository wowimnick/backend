from rest_framework import serializers
from ....models import AuditLog

class AuditLogSerializer(serializers.ModelSerializer):
    """Serializer for audit logs"""
    user_name = serializers.SerializerMethodField()
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    target_user_name = serializers.SerializerMethodField()
    target_user_role = serializers.SerializerMethodField()
    target_user_role_color = serializers.SerializerMethodField()
    user_role = serializers.SerializerMethodField()
    user_role_color = serializers.SerializerMethodField()
    
    class Meta:
        model = AuditLog
        fields = [
            'id', 'user', 'user_name', 'user_email', 'user_role', 'user_role_color',
            'action', 'action_display', 'timestamp', 
            'ip_address', 'details', 'target_user',
            'target_user_name', 'target_user_role', 'target_user_role_color',
            'target_model', 'target_id',
            'user_agent', 'metadata'
        ]
        read_only_fields = fields
    
    def get_user_name(self, obj):
        # Prevent N+1 queries by using the prefetched user if available
        if obj.user:
            return f"{obj.user.first_name} {obj.user.last_name}".strip()
        return None
    
    def get_target_user_name(self, obj):
        # Prevent N+1 queries by using the prefetched target_user if available
        if obj.target_user:
            return f"{obj.target_user.first_name} {obj.target_user.last_name}".strip()
        return None
    
    def get_action_display(self, obj):
        """Ensure consistent and properly formatted action display labels"""
        # First try to use the get_action_display method from model choices
        if hasattr(obj, 'get_action_display'):
            display = obj.get_action_display()
            if display and display != obj.action:
                return display
        
        # Fall back to formatting the raw action string
        return obj.action.replace('_', ' ').title()
    
    def get_target_user_role(self, obj):
        if obj.target_user and obj.target_user.role:
            return obj.target_user.role.name
        return None
    
    def get_target_user_role_color(self, obj):
        if obj.target_user and obj.target_user.role:
            return obj.target_user.role.color
        return None
    
    def get_user_role(self, obj):
        if obj.user and obj.user.role:
            return obj.user.role.name
        return None
    
    def get_user_role_color(self, obj):
        if obj.user and obj.user.role:
            return obj.user.role.color
        return None