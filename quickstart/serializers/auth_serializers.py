from rest_framework import serializers
from dj_rest_auth.registration.serializers import RegisterSerializer
from dj_rest_auth.serializers import LoginSerializer as DefaultLoginSerializer
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from ..models import Role, ClassesMain

User = get_user_model()

class CustomLoginSerializer(DefaultLoginSerializer):
    def get_fields(self):
        fields = super().get_fields()
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return attrs

class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ('id', 'name')

class CustomUserDetailsSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
    avatar = serializers.ImageField(write_only=True, required=False)
    role = serializers.CharField(source='role.name', read_only=True)

    class Meta:
        model = User
        fields = (
            'userId', 'email', 'first_name', 'last_name', 
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode', 
            'avatar', 'avatar_url', 'role', 'favorites'
        )
        read_only_fields = ('userId', 'email', 'role', 'avatar_url')

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, 'url'):
            return obj.avatar.url
        return None

    def update(self, instance, validated_data):
        avatar = validated_data.pop('avatar', None)
        instance = super().update(instance, validated_data)
        
        if avatar:
            if instance.avatar:
                instance.avatar.delete(save=False)
            instance.avatar = avatar
            instance.save()
            
        return instance

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = {
            'userId': self.user.userId,
            'email': self.user.email,
            'first_name': self.user.first_name,
            'last_name': self.user.last_name,
            'birth_date': self.user.birth_date,
            'bio': self.user.bio,
            'phone_number': self.user.phone_number,
            'avatar_url': self.user.get_avatar_url(),
            'role': self.user.role.name,
            'favorited': self.user.favorited.all().values_list('classId', flat=True) if self.user and hasattr(self.user, 'favorited') else []
        }
        data['role'] = self.user.role.name if self.user.role else None
        return data

class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=True)
    last_name = serializers.CharField(required=True)
    birth_date = serializers.DateField(required=True)
    phone_number = serializers.CharField(required=True)
    bio = serializers.CharField(required=False)
    country = serializers.CharField(required=True)
    state = serializers.CharField(required=True)
    city = serializers.CharField(required=True)
    address = serializers.CharField(required=True)
    zipCode = serializers.CharField(required=True)
    avatar = serializers.ImageField(required=False)
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all(), required=False)
    favorited = serializers.PrimaryKeyRelatedField(queryset=ClassesMain.objects.all(), many=True, required=False)

    def get_cleaned_data(self):
        data = super().get_cleaned_data()
        data.update({
            'first_name': self.validated_data.get('first_name', ''),
            'last_name': self.validated_data.get('last_name', ''),
            'birth_date': self.validated_data.get('birth_date', None),
            'phone_number': self.validated_data.get('phone_number', ''),
            'bio': self.validated_data.get('bio', ''),
            'country': self.validated_data.get('country', ''),
            'state': self.validated_data.get('state', ''),
            'city': self.validated_data.get('city', ''),
            'address': self.validated_data.get('address', ''),
            'zipCode': self.validated_data.get('zipCode', ''),
            'avatar': self.validated_data.get('avatar', None),
            'role': self.validated_data.get('role', None),
            'favorited': self.validated_data.get('favorited', [])
        })
        return data

    def save(self, request):
        user = super().save(request)
        user.first_name = self.validated_data.get('first_name')
        user.last_name = self.validated_data.get('last_name')
        user.birth_date = self.validated_data.get('birth_date')
        user.phone_number = self.validated_data.get('phone_number')
        user.bio = self.validated_data.get('bio')
        user.country = self.validated_data.get('country')
        user.state = self.validated_data.get('state')
        user.city = self.validated_data.get('city')
        user.address = self.validated_data.get('address')
        user.zipCode = self.validated_data.get('zipCode')
        user.avatar = self.validated_data.get('avatar')
        user.role = self.validated_data.get('role')
        user.favorited.set(self.validated_data.get('favorited', []))
        if not user.role:
            default_role = Role.objects.get(name='Student')
            user.role = default_role
        user.save()
        return user