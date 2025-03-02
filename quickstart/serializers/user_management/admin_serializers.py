from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Count
from ...models import Role

User = get_user_model()

class AdminUserListSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source='role.name', read_only=True)
    status = serializers.SerializerMethodField()
    last_login_date = serializers.DateTimeField(source='last_login', read_only=True)
    bookings_count = serializers.IntegerField(read_only=True)
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'userId', 'email', 'first_name', 'last_name', 
            'role', 'role_name', 'status', 'createdAt', 
            'last_login_date', 'phone_number', 'country', 
            'city', 'bookings_count', 'avatar_url'
        ]
    
    def get_status(self, obj):
        # This is a placeholder - implement logic based on your user model
        if hasattr(obj, 'is_active') and not obj.is_active:
            return 'inactive'
        
        # Additional logic for 'pending' or other states
        if not obj.last_login:
            return 'pending'
            
        return 'active'
    
    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
            return obj.avatar.url
        return None

class AdminUserDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for user management"""
    role_name = serializers.CharField(source='role.name', read_only=True)
    status = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'userId', 'email', 'first_name', 'last_name', 
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode', 
            'role', 'role_name', 'status', 'avatar_url',
            'createdAt', 'last_login'
        ]
    
    def get_status(self, obj):
        # Same as in list serializer
        if hasattr(obj, 'is_active') and not obj.is_active:
            return 'inactive'
        
        if not obj.last_login:
            return 'pending'
            
        return 'active'
    
    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
            return obj.avatar.url
        return None

class AdminUserCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating users by admins"""
    password = serializers.CharField(write_only=True, required=False)
    avatar = serializers.ImageField(write_only=True, required=False)
    send_welcome_email = serializers.BooleanField(write_only=True, default=True)
    
    class Meta:
        model = User
        fields = [
            'email', 'first_name', 'last_name', 
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode', 
            'role', 'password', 'avatar', 'send_welcome_email'
        ]
    
    def validate_email(self, value):
        # Check if email already exists on create
        if self.instance is None:  # Create operation
            if User.objects.filter(email=value).exists():
                raise serializers.ValidationError("A user with this email already exists.")
        return value
    
    def create(self, validated_data):
        send_welcome_email = validated_data.pop('send_welcome_email', True)
        password = validated_data.pop('password', None)
        avatar = validated_data.pop('avatar', None)
        
        # Create the user
        user = User.objects.create(**validated_data)
        
        # Set password if provided
        if password:
            user.set_password(password)
            user.save()
        
        # Set avatar if provided
        if avatar:
            user.avatar = avatar
            user.save()
        
        # Send welcome email if requested
        if send_welcome_email:
            # Implement email sending logic here
            pass
        
        return user
    
    def update(self, instance, validated_data):
        # Remove fields that should be handled separately
        password = validated_data.pop('password', None)
        avatar = validated_data.pop('avatar', None)
        validated_data.pop('send_welcome_email', None)  # Not needed for update
        
        # Update the user fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Set password if provided
        if password:
            instance.set_password(password)
        
        # Update avatar if provided
        if avatar:
            # Delete old avatar if exists
            if instance.avatar:
                instance.avatar.delete(save=False)
            
            instance.avatar = avatar
        
        instance.save()
        return instance
