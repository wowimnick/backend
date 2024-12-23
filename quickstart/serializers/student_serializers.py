from rest_framework import serializers
from ..models import (
    Student, StudentNote, Attendance, 
    Performance, Enrollment
)
from .auth_serializers import CustomUserDetailsSerializer
from .booking_serializers import BookingSerializer

class StudentNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = StudentNote
        fields = ('id', 'author', 'author_name', 'content', 'created_at')

    def get_author_name(self, obj):
        return f"{obj.author.first_name} {obj.author.last_name}" if obj.author else "Unknown"

class AttendanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attendance
        fields = ('id', 'date', 'status')

class PerformanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Performance
        fields = ('id', 'date', 'score', 'comments')

class EnrollmentSerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(source='class_instance.className', read_only=True)
    attendances = AttendanceSerializer(many=True, read_only=True)
    performances = PerformanceSerializer(many=True, read_only=True)

    class Meta:
        model = Enrollment
        fields = ('id', 'class_instance', 'class_name', 'enrollment_date', 'status', 'attendances', 'performances')

class StudentSerializer(serializers.ModelSerializer):
    user = CustomUserDetailsSerializer(read_only=True)
    userId = serializers.IntegerField(write_only=True)
    notes = StudentNoteSerializer(many=True, read_only=True)
    enrollments = EnrollmentSerializer(many=True, read_only=True)
    bookings = BookingSerializer(many=True, read_only=True)

    class Meta:
        model = Student
        fields = (
            'id', 'user', 'userId', 'enrollment_date', 'grade_level',
            'parent_guardian_name', 'parent_guardian_phone', 'emergency_contact',
            'emergency_phone', 'allergies', 'medical_conditions', 'active_classes',
            'total_classes_taken', 'average_attendance', 'overall_performance',
            'notes', 'enrollments', 'bookings'
        )
        read_only_fields = ('active_classes', 'total_classes_taken', 'average_attendance', 'overall_performance')