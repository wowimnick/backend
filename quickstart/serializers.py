# serializers.py
from rest_framework import serializers
from .models import BusinessInfo, ClassesMain, Users, SubClasses

class SubClassesSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubClasses
        fields = '__all__'

class ClassesMainSerializer(serializers.ModelSerializer):
    subclasses = SubClassesSerializer(many=True, read_only=True)

    class Meta:
        model = ClassesMain
        fields = '__all__'