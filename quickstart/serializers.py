from rest_framework import serializers
from .models import BusinessInfo, ClassesMain, Users, SubClasses, Reviews, ClassImage

class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = Users
        fields = ['userId', 'name']

    def get_name(self, obj):
        return f"{obj.firstName} {obj.lastName}"

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