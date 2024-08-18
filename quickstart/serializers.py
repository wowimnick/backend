from rest_framework import serializers
from dj_rest_auth.registration.serializers import RegisterSerializer
from django.contrib.auth import get_user_model
from dj_rest_auth.serializers import LoginSerializer as DefaultLoginSerializer
from dj_rest_auth.serializers import TokenSerializer
import logging 
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import BusinessInfo, ClassesMain, CustomUser, SubClasses, Reviews, ClassImage, CustomUser

logger = logging.getLogger(__name__)
User = get_user_model()

class CustomLoginSerializer(DefaultLoginSerializer):
    def get_fields(self):
        fields = super().get_fields()
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return attrs
    
class CustomUserDetailsSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ('userId', 'email', 'first_name', 'last_name', 'birth_date', 'phone_number', 'bio', 'country', 'city', 'address', 'avatar_url')
        read_only_fields = ('userId', 'email', 'avatar_url')

    def get_avatar_url(self, obj):
        return obj.get_avatar_url()
    
class CustomTokenSerializer(TokenSerializer):
    user = CustomUserDetailsSerializer(read_only=True)

    class Meta(TokenSerializer.Meta):
        fields = ('key', 'user')

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        logger.debug(f"Validating data: {attrs}")
        data = super().validate(attrs)
        refresh = self.get_token(self.user)
        data['refresh'] = str(refresh)
        data['access'] = str(refresh.access_token)
        
        # Add CustomUser fields
        user = CustomUser.objects.get(pk=self.user.userId)
        data['user'] = {
            'userId': user.userId,
            'email': user.email,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'birth_date': user.birth_date,
            'bio': user.bio,
            'phone_number': user.phone_number,
            'country': user.country,
            'city': user.city,
            'state': user.state,
            'address': user.address,
            'zipCode': user.zipCode,
            'avatar_url': user.get_avatar_url(),
        }
        return data

class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=False)
    last_name = serializers.CharField(required=False)
    birth_date = serializers.DateField(required=False)  
    phone_number = serializers.CharField(required=False) 
    bio = serializers.CharField(required=False)
    country = serializers.CharField(required=False)
    city = serializers.CharField(required=False)
    address = serializers.CharField(required=False)
    avatar = serializers.ImageField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.debug("CustomRegisterSerializer initialized")

    def validate(self, data):
        logger.debug(f"Validating data: {data}")
        try:
            validated_data = super().validate(data)
            logger.debug(f"Data after super().validate: {validated_data}")
            return validated_data
        except serializers.ValidationError as e:
            logger.error(f"Validation error: {e.detail}")
            raise

    def get_cleaned_data(self):
        logger.debug("get_cleaned_data called")
        data = super().get_cleaned_data()
        data.update({
            'first_name': self.validated_data.get('first_name', ''),
            'last_name': self.validated_data.get('last_name', ''),
            'birth_date': self.validated_data.get('birth_date', None),
            'phone_number': self.validated_data.get('phone_number', ''),
            'bio': self.validated_data.get('bio', ''),
            'country': self.validated_data.get('country', ''),
            'city': self.validated_data.get('city', ''),
            'address': self.validated_data.get('address', ''),
            'avatar': self.validated_data.get('avatar', None)
        })
        logger.debug(f"Cleaned data: {data}")
        return data

    def save(self, request):
        logger.debug(f"save method called")
        logger.debug(f"Validated data: {self.validated_data}")
        user = super().save(request)
        user.first_name = self.validated_data.get('first_name')
        user.last_name = self.validated_data.get('last_name')
        user.birth_date = self.validated_data.get('birth_date')
        user.phone_number = self.validated_data.get('phone_number')
        user.bio = self.validated_data.get('bio')
        user.country = self.validated_data.get('country')
        user.city = self.validated_data.get('city')
        user.address = self.validated_data.get('address')
        user.avatar = self.validated_data.get('avatar')
        user.save()
        logger.debug(f"User saved: {user}")
        return user

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['username'],
            password=validated_data['password1'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            birth_date=validated_data.get('birth_date'),
            phone_number=validated_data.get('phone_number', ''),
            bio=validated_data.get('bio', ''),
            country=validated_data.get('country', ''),
            city=validated_data.get('city', ''),
            address=validated_data.get('address', ''),
            avatar=validated_data.get('avatar')
        )
        return user


    


class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['userId', 'name']

    def get_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
    
class BusinessInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessInfo
        fields = '__all__'

class ReviewSerializer(serializers.ModelSerializer):
    userId = UserSerializer(read_only=True)

    class Meta:
        model = Reviews
        fields = ['reviewId', 'userId', 'rating', 'comment', 'createdAt']

class SubClassesSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubClasses
        fields = '__all__'

class ClassImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

class ClassesMainSerializer(serializers.ModelSerializer):
    classVideo = serializers.FileField(required=False)
    subclasses = SubClassesSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)
    images = ClassImageSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__'