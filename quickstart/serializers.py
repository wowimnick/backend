from rest_framework import serializers
from .models import BusinessInfo, ClassesMain, Users, SubClasses, Reviews

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

class ClassesMainSerializer(serializers.ModelSerializer):
    subclasses = SubClassesSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__'