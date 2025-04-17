from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db.models import Count

from ....models import Booking

User = get_user_model()

class AdminUserBookingSerializer(serializers.ModelSerializer):
    """Serializer for displaying user bookings in the admin user detail view."""
    booking_id = serializers.IntegerField(source='id', read_only=True)
    class_name = serializers.CharField(source='schedule_instance.schedule.option.classId.title', read_only=True)
    class_id = serializers.IntegerField(source='schedule_instance.schedule.option.classId.classId', read_only=True)
    instance_date = serializers.DateField(source='schedule_instance.date', read_only=True)
    instance_time = serializers.TimeField(source='schedule_instance.time', read_only=True)
    amount_paid = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True) # Get human-readable status

    class Meta:
        model = Booking
        fields = [
            'booking_id',
            'class_id',
            'class_name',
            'instance_date',
            'instance_time',
            'status',       # Raw status key ('confirmed', 'completed', etc.)
            'status_display', # Human-readable status
            'booking_date', # When the booking was made
            'amount_paid',
            'participants',
        ]
        read_only_fields = fields # All fields are read-only in this context

class AdminUserListSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source='role.name', read_only=True, allow_null=True, default='Unknown Role')
    role_color = serializers.CharField(source='role.color', read_only=True, allow_null=True, default='#64748b')
    status = serializers.SerializerMethodField()
    last_login_date = serializers.DateTimeField(source='last_login', read_only=True)
    bookings_count = serializers.IntegerField(read_only=True)
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'userId', 'email', 'first_name', 'last_name',
            'role', 
            'role_name', 
            'role_color', 
            'status',
            'createdAt',
            'last_login_date',
            'phone_number', 'country', 'city', 
            'bookings_count',
            'avatar_url'
        ]
        read_only_fields = ['last_login_date', 'bookings_count', 'avatar_url', 'createdAt', 'status', 'role_name', 'role_color']
    
    def get_status(self, obj):
        """
        Determine user status based on is_active and last_login.
        Matches the logic used in the viewset filtering.
        """
        if not obj.is_active:
            return 'inactive'
        if obj.is_active and obj.last_login is None:
            return 'pending'
        return 'active'

    def get_avatar_url(self, obj):
        if hasattr(obj, 'get_avatar_url'):
             return obj.get_avatar_url()
        if obj.avatar and hasattr(obj.avatar, 'url'):
            return obj.avatar.url
        return None

class AdminUserDetailSerializer(serializers.ModelSerializer):
    """Detailed serializer for user management"""
    role_name = serializers.CharField(source='role.name', read_only=True, allow_null=True, default='Unknown Role')
    role_color = serializers.CharField(source='role.color', read_only=True, allow_null=True, default='#64748b')
    status = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = User
        fields = [
            'userId', 'email', 'first_name', 'last_name',
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode',
            'role', 
            'role_name', 
            'role_color', 
            'status',
            'avatar_url',
            'createdAt', 'last_login',
            'is_active', 
            'is_staff',
            'is_superuser',
            'date_joined', 
        ]
        read_only_fields = [
            'userId', 'status', 'avatar_url', 'createdAt', 'last_login',
            'role_name', 'role_color', 'is_active', 'is_staff',
            'is_superuser', 'date_joined'
        ]

    def get_status(self, obj):
        if not obj.is_active:
            return 'inactive'
        if obj.is_active and obj.last_login is None:
            return 'pending'
        return 'active'

    def get_avatar_url(self, obj):
        if hasattr(obj, 'get_avatar_url'):
             return obj.get_avatar_url()
        if obj.avatar and hasattr(obj.avatar, 'url'):
            return obj.avatar.url
        return None

class AdminUserCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating users by admins"""
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, style={'input_type': 'password'})
    avatar = serializers.ImageField(required=False, allow_null=True) # Allow null to potentially clear avatar

    # Email validation remains important
    email = serializers.EmailField(required=True)
    role = serializers.PrimaryKeyRelatedField(
        queryset=User.role.field.related_model.objects.all(), # Dynamically get Role model
        allow_null=True, # Allow assigning 'no role'
        required=False # Role might not be mandatory initially
    )

    class Meta:
        model = User
        fields = [
            'email', 'first_name', 'last_name',
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode',
            'role', 
            'password', 
            'avatar',   
        ]
        extra_kwargs = {
            'password': {'write_only': True, 'required': False},
            'avatar': {'required': False, 'allow_null': True} # Make avatar optional
        }

    def validate_email(self, value):
        # Case-insensitive email check
        email_lower = value.lower()
        # Check if email already exists, excluding the current instance during update
        query = User.objects.filter(email__iexact=email_lower)
        if self.instance:
            query = query.exclude(pk=self.instance.pk)
        if query.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        # Pop password and avatar to handle them explicitly
        password = validated_data.pop('password', None)
        avatar = validated_data.pop('avatar', None)

        # Create user instance (ensure required fields like username are handled if necessary)
        # If your CustomUser doesn't use 'username', this is fine.
        # If it does, you might need to generate one or require it.
        validated_data['username'] = validated_data.get('email') # Example: use email as username

        user = User.objects.create(**validated_data)

        # Set password correctly
        if password:
            user.set_password(password)

        # Assign avatar if provided
        if avatar:
            user.avatar = avatar

        user.save() # Save password hash and avatar

        # Send welcome email logic would go here if needed
        # if send_welcome_email:
        #     pass

        return user

    def update(self, instance, validated_data):
        # Pop password and avatar
        password = validated_data.pop('password', None)
        avatar = validated_data.pop('avatar', 'not-provided') # Use a sentinel value

        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        # Update password if a non-blank value was provided
        if password:
            instance.set_password(password)

        # Update avatar: set if provided, clear if explicitly set to null/empty in request
        if avatar != 'not-provided':
            if avatar is None: # Check if the request intended to clear the avatar
                 # Delete old avatar from storage if it exists
                 if instance.avatar:
                     instance.avatar.delete(save=False) # Delete file without saving model yet
                 instance.avatar = None # Set field to None
            else: # A new file was provided
                 # Delete old avatar first if replacing
                 if instance.avatar:
                     instance.avatar.delete(save=False)
                 instance.avatar = avatar # Assign new file


        instance.save()
        return instance