from rest_framework import serializers
from ..models import Booking, Student, StudentEnrollment, StudentNote

from ..serializers import CustomUserDetailsSerializer

class StudentNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()
    
    class Meta:
        model = StudentNote
        fields = [
            'id', 'content', 'created_at', 'author',
            'author_name', 'author_avatar_url'
        ]
        read_only_fields = ['created_at', 'author']
    
    def get_author_name(self, obj):
        if obj.author:
            return f"{obj.author.first_name} {obj.author.last_name}"
        return None
    
    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar:
            return obj.author.avatar.url
        return None

class StudentEnrollmentSerializer(serializers.ModelSerializer):
    class_name = serializers.SerializerMethodField()
    attendance_records = serializers.SerializerMethodField()
    
    class Meta:
        model = StudentEnrollment
        fields = [
            'id', 'class_option', 'class_name', 'start_date',
            'end_date', 'status', 'attendance_records'
        ]
    
    def get_class_name(self, obj):
        return obj.class_option.classId.title
    
    def get_attendance_records(self, obj):
        bookings = Booking.objects.filter(
            student=obj.student,
            schedule_instance__schedule__option=obj.class_option
        ).order_by('-schedule_instance__date')
        
        return [{
            'id': booking.id,
            'date': booking.schedule_instance.date,
            'class': self.get_class_name(obj),
            'status': 'Present' if booking.status == 'completed' else 'Absent'
        } for booking in bookings]
    
class StudentProfileSerializer(serializers.ModelSerializer):
    user = CustomUserDetailsSerializer(read_only=True)
    notes = StudentNoteSerializer(many=True, read_only=True)
    enrollments = StudentEnrollmentSerializer(many=True, read_only=True)
    active_classes = serializers.IntegerField(read_only=True)
    total_classes_taken = serializers.IntegerField(read_only=True)
    average_attendance = serializers.DecimalField(
        max_digits=5, decimal_places=2, read_only=True
    )

    class Meta:
        model = Student
        fields = [
            'id', 'user', 'enrollment_date', 
            'parent_guardian_name', 'parent_guardian_phone',
            'emergency_contact', 'emergency_phone',
            'allergies', 'medical_conditions',
            'active_classes', 'total_classes_taken',
            'average_attendance', 'notes', 'enrollments'
        ]
        read_only_fields = [
            'active_classes', 'total_classes_taken',
            'average_attendance', 'notes', 'enrollments'
        ]

    def create(self, validated_data):
        """
        Create a new student profile, ensuring the user is properly set
        """
        user = self.context.get('user')
        if not user:
            raise serializers.ValidationError("User is required to create a student profile")
            
        return Student.objects.create(
            user=user,
            **validated_data
        )