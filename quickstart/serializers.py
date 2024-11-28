from rest_framework import serializers
from dj_rest_auth.registration.serializers import RegisterSerializer
from django.contrib.auth import get_user_model
from dj_rest_auth.serializers import LoginSerializer as DefaultLoginSerializer
from dj_rest_auth.serializers import TokenSerializer
import logging 
from django.db.models import Sum, Count, Avg
from django.utils import timezone
from datetime import timedelta
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import Booking, BookingStatus, BusinessInfo, ClassesMain, CustomUser, Schedule, ScheduleConflict, ScheduleStudent, StudentNote, SubClasses, Reviews, ClassImage, CustomUser, Instructor, Education, Certification, Skill, InstructorNote, Role, Attendance, Performance, Enrollment, Student

logger = logging.getLogger(__name__)
User = get_user_model()

class CustomLoginSerializer(DefaultLoginSerializer):
    def get_fields(self):
        fields = super().get_fields()
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return attrs
    
class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ('id', 'name')

class CustomUserDetailsSerializer(serializers.ModelSerializer):
    avatar_url = serializers.SerializerMethodField()
    role = serializers.CharField(source='role.name', read_only=True)

    class Meta:
        model = User
        fields = (
            'userId', 'email', 'first_name', 'last_name', 
            'birth_date', 'bio', 'phone_number', 'country',
            'city', 'state', 'address', 'zipCode', 
            'avatar_url', 'role', 'favorites'
        )
        read_only_fields = ('userId', 'email', 'role')

    def get_avatar_url(self, obj):
        return obj.get_avatar_url()


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

import logging
from rest_framework import serializers

# Set up logging
logger = logging.getLogger(__name__)

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

    def validate(self, attrs):
        logger.info(f"Validating data: {attrs}")
        return attrs

    def create(self, validated_data):
        logger.info(f"Creating instructor with data: {validated_data}")
        user_id = validated_data.pop('userId', None)
        education_data = validated_data.pop('education', [])
        certification_data = validated_data.pop('certifications', [])
        skill_data = validated_data.pop('skills', [])

        if user_id is None:
            logger.error("userId is missing from validated_data")
            raise serializers.ValidationError("userId is required")
        
        try:
            user = CustomUser.objects.get(pk=user_id)
            validated_data['user'] = user
            instructor = Instructor.objects.create(**validated_data)

            for edu_item in education_data:
                Education.objects.create(instructor=instructor, **edu_item)
            
            for cert_item in certification_data:
                Certification.objects.create(instructor=instructor, **cert_item)
            
            for skill_item in skill_data:
                Skill.objects.create(instructor=instructor, **skill_item)

            logger.info(f"Instructor created: {instructor}")
            return instructor
        except CustomUser.DoesNotExist:
            logger.error(f"User with id {user_id} does not exist.")
            raise serializers.ValidationError("Invalid user ID.")

    def update(self, instance, validated_data):
        if 'userId' in validated_data:
            user_id = validated_data.pop('userId')
            try:
                user = CustomUser.objects.get(pk=user_id)
                instance.user = user
                logger.info(f"User updated for instructor: {instance.id} with new user: {user_id}")
            except CustomUser.DoesNotExist:
                logger.error(f"User with id {user_id} does not exist.")
                raise serializers.ValidationError("Invalid user ID.")

        # Handle education
        if 'education' in validated_data:
            education_data = validated_data.pop('education')
            instance.education.all().delete()  # Remove existing education
            for edu_item in education_data:
                Education.objects.create(instructor=instance, **edu_item)

        # Handle certifications
        if 'certifications' in validated_data:
            cert_data = validated_data.pop('certifications')
            instance.certifications.all().delete()  # Remove existing certifications
            for cert_item in cert_data:
                Certification.objects.create(instructor=instance, **cert_item)

        # Handle skills
        if 'skills' in validated_data:
            skill_data = validated_data.pop('skills')
            instance.skills.all().delete()  # Remove existing skills
            for skill_item in skill_data:
                Skill.objects.create(instructor=instance, **skill_item)

        # Update remaining fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        return instance

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

class BookingStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookingStatus
        fields = ['id', 'name']

class BookingSerializer(serializers.ModelSerializer):
    status = BookingStatusSerializer(read_only=True)
    status_id = serializers.IntegerField(write_only=True, required=False)
    student_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    subclass_name = serializers.SerializerMethodField()
    instructor_name = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            'id', 'student', 'student_name', 
            'instructor', 'instructor_name',
            'class_instance', 'class_name', 
            'subclass', 'subclass_name', 
            'status', 'status_id',
            'booking_date', 'class_date', 
            'cancellation_date', 'notes'
        ]

    def get_student_name(self, obj):
        return f"{obj.student.user.first_name} {obj.student.user.last_name}"

    def get_class_name(self, obj):
        return obj.class_instance.className

    def get_subclass_name(self, obj):
        return obj.subclass.subclassTitle if obj.subclass else None

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

class StudentSerializer(serializers.ModelSerializer):
    user = CustomUserDetailsSerializer(read_only=True)
    userId = serializers.IntegerField(write_only=True)
    notes = StudentNoteSerializer(many=True, read_only=True)
    enrollments = EnrollmentSerializer(many=True, read_only=True)
    bookings = BookingSerializer(many=True, read_only=True)

    class Meta:
        model = Student
        fields = (
            'id', 'user', 'userId', 'enrollment_date', 'grade_level', 'parent_guardian_name',
            'parent_guardian_phone', 'emergency_contact', 'emergency_phone', 'allergies',
            'medical_conditions', 'active_classes', 'total_classes_taken', 'average_attendance',
            'overall_performance', 'notes', 'enrollments', 'bookings'
        )
        read_only_fields = ('active_classes', 'total_classes_taken', 'average_attendance', 'overall_performance')

    def create(self, validated_data):
        user_id = validated_data.pop('userId', None)
        if user_id is None:
            raise serializers.ValidationError("userId is required")
        
        try:
            user = CustomUser.objects.get(pk=user_id)
            validated_data['user'] = user
            student = Student.objects.create(**validated_data)
            return student
        except CustomUser.DoesNotExist:
            raise serializers.ValidationError("Invalid user ID.")

    def update(self, instance, validated_data):
        if 'userId' in validated_data:
            user_id = validated_data.pop('userId')
            try:
                user = CustomUser.objects.get(pk=user_id)
                instance.user = user
            except CustomUser.DoesNotExist:
                raise serializers.ValidationError("Invalid user ID.")

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance
    
class CustomTokenSerializer(TokenSerializer):
    user = CustomUserDetailsSerializer(read_only=True)

    class Meta(TokenSerializer.Meta):
        fields = ('key', 'user')

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        
        # Add user data and role to response
        user = self.user
        data['user'] = {
            'userId': self.user.userId,
            'email': self.user.email,
            'first_name': self.user.first_name,
            'last_name': self.user.last_name,
            'birth_date': user.birth_date,
            'bio': user.bio,
            'phone_number': user.phone_number,
            'avatar_url': user.get_avatar_url(),
            'favorited': user.favorited.all().values_list('classId', flat=True) if user and hasattr(user, 'favorited') else []
        }

                # Add role information
        data['role'] = self.user.role.name if self.user.role else None
        
        return data

class CustomRegisterSerializer(RegisterSerializer):
    first_name = serializers.CharField(required=True)
    last_name = serializers.CharField(required=True)
    birth_date = serializers.DateField(required=True)  
    phone_number = serializers.CharField(required=True) 
    bio = serializers.CharField(required=False)
    country = serializers.CharField(required=True)
    state = serializers.CharField(required=True)
    city = serializers.CharField(required=True)
    address = serializers.CharField(required=True)
    zipCode = serializers.CharField(required=True)
    avatar = serializers.ImageField(required=False)
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all(), required=False)
    favorited = serializers.PrimaryKeyRelatedField(queryset=ClassesMain.objects.all(), many=True, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        logger.debug("CustomRegisterSerializer initialized")

    def validate(self, data):
        logger.debug(f"Validating data: {data}")
        try:
            validated_data = super().validate(data)
            logger.debug(f"Data after super().validate: {validated_data}")
            return validated_data
        except serializers.ValidationError as e:
            logger.error(f"Validation error: {e.detail}")
            raise

    def get_cleaned_data(self):
        logger.debug("get_cleaned_data called")
        data = super().get_cleaned_data()
        data.update({
            'first_name': self.validated_data.get('first_name', ''),
            'last_name': self.validated_data.get('last_name', ''),
            'birth_date': self.validated_data.get('birth_date', None),
            'phone_number': self.validated_data.get('phone_number', ''),
            'bio': self.validated_data.get('bio', ''),
            'country': self.validated_data.get('country', ''),
            'state': self.validated_data.get('state', ''),
            'city': self.validated_data.get('city', ''),
            'address': self.validated_data.get('address', ''),
            'zipCode': self.validated_data.get('zipCode', ''),
            'avatar': self.validated_data.get('avatar', None),
            'role': self.validated_data.get('role', None),
            'favorited': self.validated_data.get('favorited', [])
        })
        logger.debug(f"Cleaned data: {data}")
        return data

    def save(self, request):
        logger.debug(f"save method called")
        logger.debug(f"Validated data: {self.validated_data}")
        user = super().save(request)
        user.first_name = self.validated_data.get('first_name')
        user.last_name = self.validated_data.get('last_name')
        user.birth_date = self.validated_data.get('birth_date')
        user.phone_number = self.validated_data.get('phone_number')
        user.bio = self.validated_data.get('bio')
        user.country = self.validated_data.get('country')
        user.state = self.validated_data.get('state')
        user.city = self.validated_data.get('city')
        user.address = self.validated_data.get('address')
        user.zipCode = self.validated_data.get('zipCode')
        user.avatar = self.validated_data.get('avatar')
        user.role = self.validated_data.get('role')
        user.favorited = self.validated_data.get('favorited')
        if not user.role:
            default_role = Role.objects.get(name='Student')
            user.role = default_role
        user.save()
        logger.debug(f"User saved: {user}")
        return user

    def create(self, validated_data):
        user = User.objects.create_user(
            email=validated_data['email'],
            username=validated_data['username'],
            password=validated_data['password1'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            birth_date=validated_data.get('birth_date'),
            phone_number=validated_data.get('phone_number', ''),
            bio=validated_data.get('bio', ''),
            country=validated_data.get('country', ''),
            state=validated_data.get('state', ''),
            city=validated_data.get('city', ''),
            address=validated_data.get('address', ''),
            zipCode=validated_data.get('zipCode', ''),
            avatar=validated_data.get('avatar'),
            favorited=validated_data.get('favorited', [])
        )
        return user


    


class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['userId', 'name']

    def get_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"
    
class BusinessInfoSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessInfo
        fields = '__all__'

class BusinessStatsSerializer(serializers.ModelSerializer):
    total_revenue = serializers.SerializerMethodField()
    total_students = serializers.SerializerMethodField()
    total_classes = serializers.SerializerMethodField()
    total_instructors = serializers.SerializerMethodField()
    average_rating = serializers.SerializerMethodField()
    recent_bookings = serializers.SerializerMethodField()
    class_categories = serializers.SerializerMethodField()

    class Meta:
        model = BusinessInfo
        fields = [
            'businessId', 'businessName', 'totalReviews', 
            'total_revenue', 'total_students', 'total_classes',
            'total_instructors', 'average_rating', 'recent_bookings',
            'class_categories'
        ]

    def get_total_revenue(self, obj):
        # Calculate revenue from completed bookings
        return Booking.objects.filter(
            class_instance__businessId=obj,
            status__name='Completed'
        ).aggregate(
            total=Sum('class_instance__classPrice')
        )['total'] or 0

    def get_total_students(self, obj):
        # Count unique students with active bookings
        return Booking.objects.filter(
            class_instance__businessId=obj
        ).values('student').distinct().count()

    def get_total_classes(self, obj):
        # Count active classes
        return ClassesMain.objects.filter(
            businessId=obj,
            isActive=True
        ).count()

    def get_total_instructors(self, obj):
        # Count active instructors
        return Instructor.objects.filter(
            business=obj
        ).count()

    def get_average_rating(self, obj):
        # Calculate average rating from reviews
        return Reviews.objects.filter(
            classId__businessId=obj
        ).aggregate(
            avg=Avg('rating')
        )['avg'] or 0

    def get_recent_bookings(self, obj):
        # Get bookings from the last 30 days
        thirty_days_ago = timezone.now() - timedelta(days=30)
        return Booking.objects.filter(
            class_instance__businessId=obj,
            booking_date__gte=thirty_days_ago
        ).count()

    def get_class_categories(self, obj):
        # Get distribution of classes by category
        return ClassesMain.objects.filter(
            businessId=obj
        ).values('classCategory').annotate(
            count=Count('classId')
        )

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

class ScheduleStudentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.user.get_full_name', read_only=True)
    
    class Meta:
        model = ScheduleStudent
        fields = ['id', 'student', 'student_name', 'status', 'attendance_time', 'notes', 'created_at']

class ScheduleSerializer(serializers.ModelSerializer):
    instructor_name = serializers.CharField(source='instructor.user.get_full_name', read_only=True)
    class_name = serializers.CharField(source='class_instance.className', read_only=True)
    subclass_name = serializers.CharField(source='subclass.subclassTitle', read_only=True)
    students = ScheduleStudentSerializer(many=True, read_only=True)
    
    class Meta:
        model = Schedule
        fields = [
            'id', 'class_instance', 'class_name', 'subclass', 'subclass_name',
            'instructor', 'instructor_name', 'date', 'start_time', 'end_time',
            'status', 'room', 'capacity', 'enrolled_students', 'notes',
            'is_recurring', 'recurrence_pattern', 'recurrence_end_date',
            'created_at', 'updated_at', 'students'
        ]

    def validate(self, data):
        # Validate time range
        if data.get('start_time') and data.get('end_time'):
            if data['start_time'] >= data['end_time']:
                raise serializers.ValidationError("End time must be after start time")

        # Validate capacity
        if data.get('capacity', 0) < data.get('enrolled_students', 0):
            raise serializers.ValidationError("Capacity cannot be less than enrolled students")

        # Validate recurrence
        if data.get('is_recurring'):
            if not data.get('recurrence_pattern'):
                raise serializers.ValidationError("Recurrence pattern is required for recurring schedules")
            if not data.get('recurrence_end_date'):
                raise serializers.ValidationError("Recurrence end date is required for recurring schedules")
            if data['recurrence_end_date'] < data['date']:
                raise serializers.ValidationError("Recurrence end date must be after start date")

        return data

class ScheduleConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleConflict
        fields = ['id', 'schedule', 'conflicting_schedule', 'conflict_type', 'created_at']