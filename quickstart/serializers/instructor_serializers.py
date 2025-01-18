from rest_framework import serializers, viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count
from django.shortcuts import get_object_or_404

from ..models import (
    Instructor, Education, Certification, 
    Skill, InstructorNote, BusinessInfo, Schedule
)

from .auth_serializers import CustomUserDetailsSerializer

# Nested serializers
class EducationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Education
        fields = ('id', 'degree', 'institution', 'year')

class CertificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Certification
        fields = ('id', 'name')

class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = ('id', 'name')

class InstructorNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = InstructorNote
        fields = ('id', 'content', 'created_at', 'author', 'author_name', 'author_avatar_url')
        read_only_fields = ('author',)

    def get_author_name(self, obj):
        if obj.author:
            return f"{obj.author.first_name} {obj.author.last_name}"
        return None

    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar:
            return obj.author.avatar.url
        return None

class InstructorSerializer(serializers.ModelSerializer):
    user = CustomUserDetailsSerializer()  # From your existing auth serializers
    education = EducationSerializer(many=True, read_only=True)
    certifications = CertificationSerializer(many=True, read_only=True)
    skills = SkillSerializer(many=True, read_only=True)
    notes = InstructorNoteSerializer(many=True, read_only=True)
    active_classes = serializers.IntegerField(read_only=True)
    total_students = serializers.IntegerField(read_only=True)

    class Meta:
        model = Instructor
        fields = (
            'id', 'user', 'specialization', 'employment_type',
            'department', 'hire_date', 'active_classes', 
            'total_students', 'education', 'certifications', 
            'skills', 'notes', 'business'
        )
        read_only_fields = ('active_classes', 'total_students')