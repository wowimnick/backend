from rest_framework import serializers
from ..models import Booking, BookingStatus

class BookingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingStatus
        fields = ['id', 'name']

class BookingSerializer(serializers.ModelSerializer):
    status = BookingStatusSerializer(read_only=True)
    status_id = serializers.IntegerField(write_only=True, required=False)
    student_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    option_name = serializers.SerializerMethodField()
    instructor_name = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'student', 'student_name',
            'instructor', 'instructor_name',
            'class_instance', 'class_name',
            'option', 'option_name',
            'status', 'status_id',
            'booking_date', 'class_date',
            'cancellation_date', 'notes'
        ]

    def get_student_name(self, obj):
        return f"{obj.student.user.first_name} {obj.student.user.last_name}"

    def get_class_name(self, obj):
        return obj.class_instance.className

    def get_option_name(self, obj):
        return obj.option.title if obj.option else None

    def get_instructor_name(self, obj):
        if obj.instructor:
            return f"{obj.instructor.user.first_name} {obj.instructor.user.last_name}"
        return None

    def update(self, instance, validated_data):
        status_id = validated_data.pop('status_id', None)
        instructor_id = validated_data.pop('instructor', None)
        
        if status_id is not None:
            instance.status_id = status_id

        if instructor_id is not None:
            instance.instructor_id = instructor_id

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
            
        instance.save()
        return instance