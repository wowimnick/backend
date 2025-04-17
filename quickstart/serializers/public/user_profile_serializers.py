from rest_framework import serializers
from ...models import CustomUser

class MyProfileSerializer(serializers.ModelSerializer):
    """Serializer for a user viewing their OWN profile."""
    avatar_url = serializers.SerializerMethodField()
    role_name = serializers.CharField(source='role.name', read_only=True, default='Student') # Show role name

    class Meta:
        model = CustomUser
        fields = [
            'userId', 'email', 'username', 'first_name', 'last_name',
            'birth_date', 'bio', 'phone_number', 'country', 'city',
            'state', 'address', 'zipCode', 'avatar_url', 
            'role_name', 
            'createdAt', 'last_login'
            # Add favorited classes count? Other global stats?
        ]
        read_only_fields = [
            'userId', 'email', 'username', 
            'avatar_url', 'role_name', 'createdAt', 'last_login'
        ]

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
             try:
                 return obj.avatar.url
             except ValueError:
                 return None
        return None