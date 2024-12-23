from rest_framework import serializers
from ..models import Schedule, ScheduleStudent

class ScheduleStudentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.user.get_full_name', read_only=True)
    
    class Meta:
        model = ScheduleStudent
        fields = ['id', 'student', 'student_name', 'status', 'attendance_time', 'notes', 'created_at']

class ScheduleSerializer(serializers.ModelSerializer):
    instructor_name = serializers.CharField(source='instructor.user.get_full_name', read_only=True)
    class_name = serializers.CharField(source='class_instance.className', read_only=True)
    option_name = serializers.CharField(source='option.title', read_only=True)
    students = ScheduleStudentSerializer(many=True, read_only=True)
    
    class Meta:
        model = Schedule
        fields = [
            'id', 'class_instance', 'class_name', 'option', 'option_name',
            'instructor', 'instructor_name', 'date', 'start_time', 'end_time',
            'status', 'room', 'capacity', 'enrolled_students', 'notes',
            'is_recurring', 'recurrence_pattern', 'recurrence_end_date',
            'created_at', 'updated_at', 'students'
        ]

    def validate(self, data):
        if data.get('start_time') and data.get('end_time'):
            if data['start_time'] >= data['end_time']:
                raise serializers.ValidationError("End time must be after start time")

        if data.get('capacity', 0) < data.get('enrolled_students', 0):
            raise serializers.ValidationError("Capacity cannot be less than enrolled students")

        if data.get('is_recurring'):
            if not data.get('recurrence_pattern'):
                raise serializers.ValidationError("Recurrence pattern is required for recurring schedules")
            if not data.get('recurrence_end_date'):
                raise serializers.ValidationError("Recurrence end date is required for recurring schedules")
            if data['recurrence_end_date'] < data['date']:
                raise serializers.ValidationError("Recurrence end date must be after start date")

        return data