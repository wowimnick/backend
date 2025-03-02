from rest_framework import serializers
from django.contrib.auth.models import Permission
from ...models import Role, PermissionGroup, EnhancedPermission

class PermissionSerializer(serializers.ModelSerializer):
    """Serializer for permissions with enhanced metadata"""
    id = serializers.IntegerField(source='permission.id')
    name = serializers.CharField(source='permission.name')
    codename = serializers.CharField(source='permission.codename')
    
    class Meta:
        model = EnhancedPermission
        fields = [
            'id', 'name', 'codename', 'description', 
            'is_sensitive', 'requires_mfa', 'group'
        ]

class PermissionGroupSerializer(serializers.ModelSerializer):
    """Serializer for permission groups"""
    permissions = PermissionSerializer(many=True, read_only=True)
    
    class Meta:
        model = PermissionGroup
        fields = ['id', 'name', 'description', 'permissions']

class RoleDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for roles with permissions"""
    permissions = serializers.PrimaryKeyRelatedField(
        queryset=Permission.objects.all(),
        many=True,
        required=False
    )
    user_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Role
        fields = [
            'id', 'name', 'description', 'permissions', 
            'is_default', 'is_system', 'user_count', 'color',
            'hierarchy_level', 'created_at', 'updated_at'
        ]
    
    def update(self, instance, validated_data):
        permissions = validated_data.pop('permissions', None)
        
        # Update role fields
        instance = super().update(instance, validated_data)
        
        # Update permissions if provided
        if permissions is not None:
            instance.permissions.set(permissions)
        
        # If this role is set as default, unset other default roles
        if validated_data.get('is_default', False):
            Role.objects.exclude(pk=instance.pk).update(is_default=False)
        
        return instance

class RoleCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new roles"""
    permissions = serializers.PrimaryKeyRelatedField(
        queryset=Permission.objects.all(),
        many=True,
        required=False
    )
    
    class Meta:
        model = Role
        fields = [
            'name', 'description', 'permissions', 
            'is_default', 'is_system', 'color',
            'hierarchy_level'
        ]
    
    def validate_name(self, value):
        # Check if role with this name already exists
        if Role.objects.filter(name=value).exists():
            raise serializers.ValidationError("A role with this name already exists.")
        return value
    
    def create(self, validated_data):
        permissions = validated_data.pop('permissions', [])
        
        # Create the role
        role = Role.objects.create(**validated_data)
        
        # Add permissions
        if permissions:
            role.permissions.set(permissions)
        
        # If this role is set as default, unset other default roles
        if validated_data.get('is_default', False):
            Role.objects.exclude(pk=role.pk).update(is_default=False)
        
        return role