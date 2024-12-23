from rest_framework import serializers
from ..models import ClassesMain, ClassImage, ClassOption, Reviews
from .auth_serializers import CustomUserDetailsSerializer

class ClassOptionSerializer(serializers.ModelSerializer):
    images = serializers.JSONField(required=False)
    days = serializers.ListField(child=serializers.CharField(), required=False)
    equipment = serializers.ListField(child=serializers.CharField(), required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = ClassOption
        fields = '__all__'

    def validate_images(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Images must be a list")
        if value and not any(img.get('isCover') for img in value):
            value[0]['isCover'] = True
        return value

class ClassImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClassImage
        fields = ['imageId', 'image', 'createdAt']

class ReviewSerializer(serializers.ModelSerializer):
    userId = CustomUserDetailsSerializer(read_only=True)

    class Meta:
        model = Reviews
        fields = ['reviewId', 'userId', 'rating', 'comment', 'createdAt']

class ClassesMainSerializer(serializers.ModelSerializer):
    options = ClassOptionSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)
    images = ClassImageSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__'