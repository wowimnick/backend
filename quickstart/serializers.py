from rest_framework import serializers
from dj_rest_auth.registration.serializers import RegisterSerializer
from .models import BusinessInfo, ClassesMain, CustomUser, SubClasses, Reviews, ClassImage, CustomUser

class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=True)
    last_name = serializers.CharField(required=True)
    birth_date = serializers.DateField(required=False)
    phone_number = serializers.CharField(required=False)
    bio = serializers.CharField(required=False)
    country = serializers.CharField(required=False)
    city = serializers.CharField(required=False)
    address = serializers.CharField(required=False)
    avatar = serializers.ImageField(required=False)

    def custom_signup(self, request, user):
        user.first_name = self.validated_data.get('first_name', '') # type: ignore
        user.last_name = self.validated_data.get('last_name', '') # type: ignore
        user.birth_date = self.validated_data.get('birth_date', None) # type: ignore
        user.phone_number = self.validated_data.get('phone_number', '') # type: ignore
        user.bio = self.validated_data.get('bio', '') # type: ignore
        user.country = self.validated_data.get('country', '') # type: ignore
        user.city = self.validated_data.get('city', '') # type: ignore
        user.address = self.validated_data.get('address', '') # type: ignore
        user.avatar = self.validated_data.get('avatar', None) # type: ignore
        user.save()

class CustomUserDetailsSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ('id', 'email', 'first_name', 'last_name', 'birth_date', 'phone_number', 'bio', 'country', 'city', 'address', 'avatar')
        read_only_fields = ('email',)

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