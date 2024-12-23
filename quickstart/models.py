from django.db import models
from django.contrib.auth.models import AbstractUser, Permission
from django.contrib.contenttypes.models import ContentType
from storages.backends.s3boto3 import S3Boto3Storage
from decimal import Decimal

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True)

    def __str__(self):
        return self.name

class CustomUser(AbstractUser):
    userId = models.AutoField(primary_key=True)
    email = models.EmailField(unique=True)
    birth_date = models.DateField(null=True, blank=True)
    bio = models.TextField(blank=True)
    phone_number = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    address = models.CharField(max_length=255)
    zipCode = models.CharField(max_length=100)
    avatar = models.ImageField(upload_to='avatars/', storage=S3Boto3Storage(), null=True, blank=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True)
    favorited = models.ManyToManyField('ClassesMain', related_name='favorited_by', blank=True)
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return self.email
    
    def get_avatar_url(self):
        if self.avatar:
            return self.avatar.url
        return None

    def has_role(self, role_name):
        return self.role and self.role.name == role_name

class BusinessInfo(models.Model):
    businessName = models.CharField(max_length=100)
    businessId = models.AutoField(primary_key=True)
    owner = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='owned_businesses')
    managers = models.ManyToManyField(CustomUser, related_name='managed_businesses', blank=True)
    businessImage = models.ImageField(upload_to='business_images/', storage=S3Boto3Storage(), blank=True, null=True)
    businessType = models.CharField(max_length=100)
    businessAddress = models.CharField(max_length=100)
    businessDescription = models.CharField(max_length=500, blank=True, null=True)
    businessCity = models.CharField(max_length=100)
    businessState = models.CharField(max_length=100, blank=True, null=True)
    businessPhoneNumber = models.CharField(max_length=100)
    businessEmail = models.CharField(max_length=100, blank=True, null=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    businessZipCode = models.CharField(max_length=100)
    totalReviews = models.IntegerField(default=0)
    isActive = models.BooleanField(default=True)

    def update_total_reviews(self):
        self.totalReviews = Reviews.objects.filter(
            classId__businessId=self
        ).count()
        self.save()

    class Meta:
        db_table = 'businessInfo'
        verbose_name_plural = 'Business Information'

class Instructor(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='instructor_profile')
    business = models.ForeignKey(BusinessInfo, on_delete=models.CASCADE, related_name='instructors')
    specialization = models.CharField(max_length=100)
    employment_type = models.CharField(max_length=20, choices=[
        ('full-time', 'Full-time'),
        ('part-time', 'Part-time'),
        ('contract', 'Contract')
    ])
    department = models.CharField(max_length=100)
    hire_date = models.DateField()
    active_classes = models.IntegerField(default=0)
    total_students = models.IntegerField(default=0)
    performance_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    next_review_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"{self.user.first_name} {self.user.last_name}"

class Education(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='education')
    degree = models.CharField(max_length=100)
    institution = models.CharField(max_length=100)
    year = models.IntegerField()

    def __str__(self):
        return f"{self.degree} from {self.institution}"

class Certification(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='certifications')
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

class Skill(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='skills')
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name

class InstructorNote(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.instructor} by {self.author}"
    
class BookingStatus(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name

class Booking(models.Model):
    id = models.AutoField(primary_key=True)
    student = models.ForeignKey('Student', on_delete=models.CASCADE, related_name='bookings')
    instructor = models.ForeignKey('Instructor', on_delete=models.SET_NULL, null=True, related_name='bookings')
    class_instance = models.ForeignKey('ClassesMain', on_delete=models.CASCADE)
    option = models.ForeignKey('ClassOption', on_delete=models.SET_NULL, null=True, blank=True)
    status = models.ForeignKey(BookingStatus, on_delete=models.SET_NULL, null=True, blank=True)
    booking_date = models.DateTimeField(auto_now_add=True)
    class_date = models.DateTimeField()
    cancellation_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Booking {self.id} - {self.student} - {self.class_instance}"

class Student(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='student_profile')
    enrollment_date = models.DateField()
    grade_level = models.CharField(max_length=20)
    parent_guardian_name = models.CharField(max_length=100)
    parent_guardian_phone = models.CharField(max_length=20)
    emergency_contact = models.CharField(max_length=100)
    emergency_phone = models.CharField(max_length=20)
    allergies = models.TextField(blank=True)
    medical_conditions = models.TextField(blank=True)
    active_classes = models.IntegerField(default=0)
    total_classes_taken = models.IntegerField(default=0)
    average_attendance = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    overall_performance = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        return f"{self.user.first_name} {self.user.last_name}"

class StudentNote(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.student} by {self.author}"

class Enrollment(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='enrollments')
    class_instance = models.ForeignKey('ClassesMain', on_delete=models.CASCADE)
    enrollment_date = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=[
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('withdrawn', 'Withdrawn')
    ])

    def __str__(self):
        return f"{self.student} enrolled in {self.class_instance}"

class Attendance(models.Model):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField()
    status = models.CharField(max_length=20, choices=[
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('late', 'Late')
    ])

    def __str__(self):
        return f"{self.enrollment.student} - {self.date} - {self.status}"

class Performance(models.Model):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.CASCADE, related_name='performances')
    date = models.DateField()
    score = models.DecimalField(max_digits=5, decimal_places=2)
    comments = models.TextField(blank=True)

    def __str__(self):
        return f"{self.enrollment.student} - {self.date} - Score: {self.score}"

class ClassImage(models.Model):
    imageId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='class_images/', storage=S3Boto3Storage())
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'classImages'

class ClassesMain(models.Model):
    classId = models.AutoField(primary_key=True)
    businessId = models.ForeignKey(BusinessInfo, on_delete=models.CASCADE, related_name='classes')
    instructor = models.ForeignKey(Instructor, on_delete=models.SET_NULL, null=True, related_name='classes')
    className = models.CharField(max_length=50)
    classDescription = models.CharField(max_length=2000, blank=True, null=True)
    classLocation = models.CharField(max_length=100)
    classCoordinates = models.CharField(max_length=100)
    saltLocation = models.BooleanField(default=False)
    classRating = models.DecimalField(max_digits=3, decimal_places=1, default=Decimal('0.0'))
    classFeatures = models.TextField(default='[]')  # Store JSON as text
    classPrice = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    classCategory = models.CharField(max_length=100)
    classFilterCategory = models.CharField(max_length=100)
    classFilterSubcategory = models.CharField(max_length=100)
    classTotalReviews = models.IntegerField(default=0)
    isActive = models.BooleanField(default=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)
    studentContactEmail = models.CharField(max_length=100, blank=True, null=True)
    studentContactPhone = models.CharField(max_length=100, blank=True, null=True)
    adminContactEmail = models.CharField(max_length=100, blank=True, null=True)
    adminContactPhone = models.CharField(max_length=100, blank=True, null=True)

    class Meta:
        db_table = 'classesMain'
        ordering = ['-createdAt']

class Favorites(models.Model):
    favoriteId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='favorites')
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'favorites'

class Reviews(models.Model):
    reviewId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='reviews')
    businessId = models.ForeignKey(BusinessInfo, models.DO_NOTHING, db_column='businessId', blank=True, null=True)
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    rating = models.IntegerField()
    comment = models.TextField(blank=True, null=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.classId and self.classId.businessId:
            self.classId.businessId.update_total_reviews()

    class Meta:
        db_table = 'reviews'

class Schedule(models.Model):
    id = models.AutoField(primary_key=True)
    class_instance = models.ForeignKey('ClassesMain', on_delete=models.CASCADE, related_name='schedules')
    option = models.ForeignKey('ClassOption', on_delete=models.SET_NULL, null=True, blank=True)
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE)
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    status = models.CharField(max_length=20, choices=[
        ('active', 'Active'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed')
    ])
    room = models.CharField(max_length=100, blank=True, null=True)
    capacity = models.IntegerField()
    enrolled_students = models.IntegerField(default=0)
    notes = models.TextField(blank=True)
    is_recurring = models.BooleanField(default=False)
    recurrence_pattern = models.CharField(max_length=20, choices=[
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly')
    ], blank=True, null=True)
    recurrence_end_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date', 'start_time']
        indexes = [
            models.Index(fields=['date', 'start_time']),
            models.Index(fields=['instructor']),
            models.Index(fields=['status']),
        ]

class ScheduleStudent(models.Model):
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='students')
    student = models.ForeignKey('Student', on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=[
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('late', 'Late'),
    ])
    attendance_time = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('schedule', 'student')
        indexes = [
            models.Index(fields=['schedule', 'student']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.student} - {self.schedule}"

class ScheduleConflict(models.Model):
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='conflicts')
    conflicting_schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='conflicting_with')
    conflict_type = models.CharField(max_length=20, choices=[
        ('instructor', 'Instructor Double Booking'),
        ('room', 'Room Double Booking'),
        ('student', 'Student Double Booking'),
    ])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('schedule', 'conflicting_schedule')

class ClassOption(models.Model):
    optionId = models.AutoField(primary_key=True)
    classId = models.ForeignKey(ClassesMain, on_delete=models.CASCADE, related_name='options')
    
    # Basic Info
    title = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    images = models.TextField(default='[]')  # Store JSON as text
    
    # Schedule
    time = models.TimeField(null=True)
    frequency = models.CharField(max_length=20, choices=[
        ('weekly', 'Weekly'),
        ('biweekly', 'Bi-weekly'),
        ('monthly', 'Monthly')
    ])
    
    # Capacity & Level
    maxParticipants = models.IntegerField(null=True)
    level = models.CharField(max_length=20, choices=[
        ('beginner', 'Beginner'),
        ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'),
        ('all', 'All Levels')
    ])
    
    # Pricing & Policies
    price = models.DecimalField(max_digits=10, decimal_places=2)
    type = models.CharField(max_length=20, choices=[
        ('single', 'Per Class'),
        ('course', 'Full Course'),
        ('month', 'Per Month')
    ])
    cancellationPolicy = models.CharField(max_length=30, choices=[
        ('24h', '24 Hours Notice'),
        ('48h', '48 Hours Notice'),
        ('72h', '72 Hours Notice'),
        ('flexible', 'Flexible')
    ])
    
    # Image handling
    image = models.ImageField(
        upload_to='class_options/',
        storage=S3Boto3Storage(),
        blank=True,
        null=True
    )
    
    # Timestamps
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'class_options'
        ordering = ['createdAt']

# New models to replace ArrayField relationships
class ClassOptionDay(models.Model):
    option = models.ForeignKey(ClassOption, on_delete=models.CASCADE, related_name='days')
    day = models.CharField(max_length=3)  # e.g., 'MON', 'TUE', etc.

    class Meta:
        unique_together = ('option', 'day')

class ClassOptionEquipment(models.Model):
    option = models.ForeignKey(ClassOption, on_delete=models.CASCADE, related_name='equipment')
    equipment = models.CharField(max_length=100)

    class Meta:
        unique_together = ('option', 'equipment')

class ClassOptionTag(models.Model):
    option = models.ForeignKey(ClassOption, on_delete=models.CASCADE, related_name='tags')
    tag = models.CharField(max_length=50)

    class Meta:
        unique_together = ('option', 'tag')