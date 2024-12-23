from rest_framework import serializers
from ..models import (
    Instructor, Education, Certification, 
    Skill, InstructorNote
)
from .auth_serializers import CustomUserDetailsSerializer

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

    class Meta:
        model = InstructorNote
        fields = ('id', 'author', 'author_name', 'content', 'created_at')

    def get_author_name(self, obj):
        return f"{obj.author.first_name} {obj.author.last_name}" if obj.author else "Unknown"

class InstructorSerializer(serializers.ModelSerializer):
    user = CustomUserDetailsSerializer(read_only=True)
    userId = serializers.IntegerField(write_only=True)
    education = EducationSerializer(many=True, required=False)
    certifications = CertificationSerializer(many=True, required=False)
    skills = SkillSerializer(many=True, required=False)
    notes = InstructorNoteSerializer(many=True, read_only=True)

    class Meta:
        model = Instructor
        fields = (
            'id', 'user', 'userId', 'specialization', 'employment_type',
            'department', 'hire_date', 'active_classes', 'total_students',
            'performance_score', 'next_review_date', 'education',
            'certifications', 'skills', 'notes'
        )
        read_only_fields = ('active_classes', 'total_students', 'performance_score')