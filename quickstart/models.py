from datetime import timedelta
import json
from django.db import models
from django.contrib.auth.models import AbstractUser, Permission
from django.contrib.contenttypes.models import ContentType
from storages.backends.s3boto3 import S3Boto3Storage
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.db.models import Count
from decimal import Decimal
from django.utils import timezone
import uuid

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True)

    def __str__(self):
        return self.name
    
    class Meta:
        db_table = 'roles'

class CustomUser(AbstractUser):
    userId = models.AutoField(primary_key=True)
    email = models.EmailField(unique=True)
    birth_date = models.DateField(null=True, blank=True)
    bio = models.TextField(null=True, blank=True)
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
    
    class Meta:
        db_table = 'users'

class BusinessInfo(models.Model):
    # Basic Info (BusinessInfoStep)
    businessId = models.AutoField(primary_key=True)
    businessName = models.CharField(max_length=100)
    businessType = models.CharField(max_length=50, choices=[
        ('individual', 'Individual Teacher'),
        ('school', 'School'),
        ('studio', 'Studio'),
        ('academy', 'Academy'),
        ('center', 'Learning Center')
    ])
    businessDescription = models.TextField(max_length=500)
    businessImage = models.ImageField(upload_to='business_images/', storage=S3Boto3Storage(), blank=True, null=True)
    openingTime = models.TimeField()
    closingTime = models.TimeField()
    cancellationPolicy = models.CharField(max_length=20, choices=[
        ('24h', '24 Hours Notice'),
        ('48h', '48 Hours Notice'),
        ('72h', '72 Hours Notice'),
        ('flexible', 'Flexible')
    ])
    liabilityWaiver = models.BooleanField(default=False)

    # Contact Details
    studentContactPhone = models.CharField(max_length=100)
    studentContactEmail = models.EmailField()
    adminContactPhone = models.CharField(max_length=100, blank=True, null=True)
    adminContactEmail = models.EmailField(blank=True, null=True)
    preferredContact = models.CharField(max_length=20, choices=[
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('both', 'Both Email and Phone')
    ])

    # Location
    businessAddress = models.CharField(max_length=255)
    businessCity = models.CharField(max_length=100)
    businessState = models.CharField(max_length=100)
    businessZipCode = models.CharField(max_length=20)
    latitude = models.DecimalField(max_digits=10, decimal_places=8, null=True)
    longitude = models.DecimalField(max_digits=11, decimal_places=8, null=True)
    showExactLocation = models.BooleanField(default=False)

    # Class Types
    classCategory = models.CharField(max_length=50, choices=[
        ('academic', 'Academic'),
        ('music', 'Music'),
        ('dance', 'Dance'),
        ('fitness', 'Fitness'),
        ('art', 'Art'),
        ('technology', 'Technology'),
        ('sports', 'Sports')
    ])
    subcategories = models.CharField(max_length=255)  # Comma-separated
    classFormats = models.CharField(max_length=255)   # Comma-separated
    skillLevels = models.CharField(max_length=255)    # Comma-separated
    ageGroups = models.CharField(max_length=255)      # Comma-separated

    # Verification
    verificationDocument = models.FileField(
        upload_to='verification_documents/',
        storage=S3Boto3Storage(),
        blank=True,
        null=True
    )
    verificationStatus = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected')
    ], default='pending')
    termsAccepted = models.BooleanField(default=False)
    privacyAccepted = models.BooleanField(default=False)

    # Relations and Metadata
    owner = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='owned_businesses')
    managers = models.ManyToManyField(CustomUser, related_name='managed_businesses', blank=True)
    isActive = models.BooleanField(default=True)
    totalReviews = models.IntegerField(default=0)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)


    def __str__(self):
        return self.businessName

    def get_subcategories(self):
        return [x.strip() for x in self.subcategories.split(',')] if self.subcategories else []

    def get_class_formats(self):
        return [x.strip() for x in self.classFormats.split(',')] if self.classFormats else []

    def get_skill_levels(self):
        return [x.strip() for x in self.skillLevels.split(',')] if self.skillLevels else []

    def get_age_groups(self):
        return [x.strip() for x in self.ageGroups.split(',')] if self.ageGroups else []

    def update_total_reviews(self):
        """Update total reviews count efficiently using annotation"""
        from .models import Reviews
        total = Reviews.objects.filter(
            classId__businessId=self.businessId
        ).aggregate(
            total=Count('reviewId')
        )['total']
        
        # Only update if count has changed
        if total != self.totalReviews:
            BusinessInfo.objects.filter(businessId=self.businessId).update(
                totalReviews=total
            )
            self.totalReviews = total
    class Meta:
        db_table = 'business_info'
        verbose_name_plural = 'Business Information'
        indexes = [
            models.Index(fields=['businessType']),
            models.Index(fields=['classCategory']),
            models.Index(fields=['isActive']),
        ]


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
    
    class Meta:
        db_table = 'instructors'

class Education(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='education')
    degree = models.CharField(max_length=100)
    institution = models.CharField(max_length=100)
    year = models.IntegerField()

    def __str__(self):
        return f"{self.degree} from {self.institution}"
    
    class Meta:
        db_table = 'instructor_education'

class Certification(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='certifications')
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name
    
    class Meta:
        db_table = 'instructor_certifications'

class Skill(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='skills')
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name
    
    class Meta:
        db_table = 'instructor_skills'

class InstructorNote(models.Model):
    instructor = models.ForeignKey(Instructor, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.instructor} by {self.author}"
    
    class Meta:
        db_table = 'instructor_notes'
    


class Student(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='student_profile')
    enrollment_date = models.DateField()
    parent_guardian_name = models.CharField(max_length=100)
    parent_guardian_phone = models.CharField(max_length=20)
    emergency_contact = models.CharField(max_length=100)
    emergency_phone = models.CharField(max_length=20)
    allergies = models.TextField(blank=True)
    medical_conditions = models.TextField(blank=True)

    def __str__(self):
        return f"{self.user.first_name} {self.user.last_name}"
    
    @property
    def active_classes(self):
        """Count of currently confirmed bookings"""
        return self.bookings.filter(status='confirmed').count()
    
    @property
    def total_classes_taken(self):
        """Count of completed bookings"""
        return self.bookings.filter(status='completed').count()
    
    @property
    def average_attendance(self):
        """Calculate attendance rate from completed vs total finished bookings"""
        total_finished = self.bookings.filter(
            status__in=['completed', 'cancelled']
        ).count()
        if total_finished == 0:
            return Decimal('0.00')
        completed = self.bookings.filter(status='completed').count()
        return Decimal(str(round((completed / total_finished) * 100, 2)))
    
    class Meta:
        db_table = 'students'

class StudentNote(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.student} by {self.author}"
    
    class Meta:
        db_table = 'student_notes'

class ClassImage(models.Model):
    imageId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='class_images/', storage=S3Boto3Storage())
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'class_images'

class ClassesMain(models.Model):
    classId = models.AutoField(primary_key=True)
    businessId = models.ForeignKey('BusinessInfo', on_delete=models.CASCADE)
    
    # Basic Info Fields
    title = models.CharField(max_length=100)
    description = models.TextField(max_length=2000)
    features = models.JSONField(default=list)  # Store as JSON array
    category = models.CharField(max_length=50)
    subcategory = models.CharField(max_length=50, null=True, blank=True)
    
    # Location Fields
    location = models.CharField(max_length=255)
    coordinates = models.CharField(max_length=50)  # "lat,long" format
    saltLocation = models.BooleanField(default=False)
    
    # Contact Fields
    studentContactEmail = models.EmailField(null=True, blank=True)
    studentContactPhone = models.CharField(max_length=20, null=True, blank=True)
    adminContactEmail = models.EmailField(null=True, blank=True)
    adminContactPhone = models.CharField(max_length=20, null=True, blank=True)
    
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'classes'

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

class ClassOption(models.Model):
    optionId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', on_delete=models.CASCADE, related_name='options')

    # Basic Info
    title = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    
    BOOKING_TYPES = [
        ('single', 'Single Session'),
        ('course', 'Full Course'),
        ('recurring', 'Recurring Classes')
    ]
    booking_type = models.CharField(
        max_length=20,
        choices=BOOKING_TYPES,
        default='single'
    )

    image = models.ImageField(
        upload_to='class_options/', 
        storage=S3Boto3Storage(),
        null=True,
        blank=True
    )
    
    # Common Fields
    duration = models.IntegerField(default=60)
    maxParticipants = models.IntegerField(
        validators=[MinValueValidator(1)],
        null=True,
        blank=True
    )

    level = models.CharField(
        max_length=20,
        choices=[
            ('beginner', 'Beginner'),
            ('intermediate', 'Intermediate'),
            ('advanced', 'Advanced'),
            ('all', 'All Levels')
        ]
    )

    # Pricing
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    PRICE_TYPES = [
        ('per_session', 'Per Session'),
        ('per_month', 'Monthly'),
        ('full_course', 'Full Course')
    ]
    price_type = models.CharField(
        max_length=20, 
        choices=PRICE_TYPES,
        default='per_session'
    )

    # Course Specific
    total_sessions = models.IntegerField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    # Recurring Specific
    RECURRENCE_PATTERNS = [
        ('weekly', 'Weekly'),
        ('biweekly', 'Bi-weekly'),
        ('monthly', 'Monthly')
    ]
    recurrence_pattern = models.CharField(
        max_length=20,
        choices=RECURRENCE_PATTERNS,
        null=True,
        blank=True
    )
    sessions_per_week = models.IntegerField(null=True, blank=True)
    auto_renew_default = models.BooleanField(default=False)
    
    # Additional Info
    equipment = models.JSONField(default=list)
    tags = models.JSONField(default=list)
    cancellationPolicy = models.CharField(
        max_length=30,
        choices=[
            ('24h', '24 Hours Notice'),
            ('48h', '48 Hours Notice'),
            ('72h', '72 Hours Notice'),
            ('flexible', 'Flexible')
        ]
    )
    active = models.BooleanField(default=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    def get_image_url(self):
        if self.image:
            return self.image.url
        return None
    
    class Meta:
        db_table = 'class_options'

class Schedule(models.Model):
    """Template for recurring schedules"""
    option = models.ForeignKey(ClassOption, on_delete=models.CASCADE, related_name='schedules')
    day = models.CharField(
        max_length=3,
        choices=[
            ('Mon', 'Monday'),
            ('Tue', 'Tuesday'),
            ('Wed', 'Wednesday'),
            ('Thu', 'Thursday'),
            ('Fri', 'Friday'),
            ('Sat', 'Saturday'),
            ('Sun', 'Sunday')
        ]
    )
    time = models.TimeField()
    price = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Override price for this schedule. If not set, uses ClassOption default price"
    )
    maxParticipants = models.IntegerField(
        null=True, 
        blank=True,
        help_text="Override max participants for this schedule. If not set, uses ClassOption default"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def effective_max_participants(self):
        return self.maxParticipants if self.maxParticipants is not None else self.option.maxParticipants

    @property
    def effective_price(self):
        return self.price if self.price is not None else self.option.price

    def generate_instances(self, start_date, weeks_ahead=12):
        day_to_number = {
            'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 
            'Fri': 4, 'Sat': 5, 'Sun': 6
        }
        target_weekday = day_to_number[self.day]
        
        current_date = start_date
        while current_date.weekday() != target_weekday:
            current_date += timedelta(days=1)
        
        end_date = start_date + timedelta(weeks=weeks_ahead)
        instances = []
        
        while current_date < end_date:
            # Check if an instance already exists for this date
            existing = ScheduleInstance.objects.filter(
                schedule=self,
                date=current_date
            ).exists()
            
            if not existing:
                instance = ScheduleInstance(
                    schedule=self,
                    date=current_date,
                    time=self.time,
                    price=self.effective_price,
                    max_participants=self.effective_max_participants
                )
                instances.append(instance)
            current_date += timedelta(weeks=1)
            
        return ScheduleInstance.objects.bulk_create(instances)

    class Meta:
        db_table = 'schedules'
        ordering = ['day', 'time']
        indexes = [
            models.Index(fields=['option', 'day', 'time']),
        ]

class ScheduleInstance(models.Model):
    """Specific occurrence of a schedule"""
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='instances')
    date = models.DateField()
    time = models.TimeField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    max_participants = models.IntegerField()
    
    STATUS_CHOICES = [
        ('scheduled', 'Scheduled'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed')
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')
    cancellation_reason = models.TextField(blank=True)
    
    # Attendance tracking
    attendance_marked = models.BooleanField(default=False)
    instructor_notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def current_bookings(self):
        return self.bookings.filter(
            status='confirmed'
        ).aggregate(
            total=models.Sum('participants')
        )['total'] or 0
    
    @property
    def available_spots(self):
        return self.max_participants - self.current_bookings

    def can_accommodate(self, requested_participants):
        return self.available_spots >= requested_participants

    class Meta:
        db_table = 'schedule_instances'
        unique_together = ['schedule', 'date']
        indexes = [
            models.Index(fields=['date', 'time']),
            models.Index(fields=['schedule', 'date']),
        ]

class ScheduleBreak(models.Model):
    """Defines break periods for schedules"""
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='breaks')
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Automatically cancel all instances in this date range
        instances = ScheduleInstance.objects.filter(
            schedule=self.schedule,
            date__range=(self.start_date, self.end_date),
            status='scheduled'
        )
        instances.update(
            status='cancelled',
            cancellation_reason=f"Break period: {self.reason}"
        )
    
    class Meta:
        db_table = 'schedule_breaks'
        indexes = [
            models.Index(fields=['schedule', 'start_date', 'end_date']),
        ]

class Attendance(models.Model):
    """Tracks attendance for schedule instances"""
    schedule_instance = models.ForeignKey(ScheduleInstance, on_delete=models.CASCADE, related_name='attendance_records')
    student = models.ForeignKey('Student', on_delete=models.CASCADE, related_name='attendance_records')
    STATUS_CHOICES = [
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('late', 'Late')
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    notes = models.TextField(blank=True)
    marked_at = models.DateTimeField(auto_now_add=True)
    marked_by = models.ForeignKey('CustomUser', on_delete=models.SET_NULL, null=True)

    class Meta:
        db_table = 'attendance'
        unique_together = ['schedule_instance', 'student']
        indexes = [
            models.Index(fields=['schedule_instance', 'student']),
            models.Index(fields=['status', 'marked_at']),
        ]

class Booking(models.Model):
    id = models.AutoField(primary_key=True)
    booking_group_id = models.UUIDField(null=True, blank=True)
    schedule_instance = models.ForeignKey(ScheduleInstance, on_delete=models.CASCADE, related_name='bookings')
    student = models.ForeignKey('Student', on_delete=models.CASCADE, related_name='bookings')
    participants = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(4)])
    notes = models.TextField(blank=True)
    
    # Enrollment tracking
    enrollment_type = models.CharField(
        max_length=20, 
        choices=[
            ('Single Session', 'Single Session'),
            ('Full Course', 'Full Course'),
            ('Recurring Classes', 'Recurring Classes')
        ], 
        default='Single Session'
    )
    course_start_date = models.DateField(null=True, blank=True)
    course_end_date = models.DateField(null=True, blank=True)
    total_sessions = models.IntegerField(null=True, blank=True)
    sessions_per_week = models.IntegerField(null=True, blank=True)
    recurrence_pattern = models.CharField(max_length=20, choices=[
        ('weekly', 'Weekly'),
        ('biweekly', 'Bi-weekly')
    ], null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'), 
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed')
    ], default='pending')
    
    booking_date = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)
    
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('refunded', 'Refunded')
    ], default='pending')

    attendance_marked = models.BooleanField(default=False)
    attended = models.BooleanField(default=False)

    class Meta:
        db_table = 'bookings'
        indexes = [
            models.Index(fields=['schedule_instance', 'status']),
            models.Index(fields=['student', 'status']),
            models.Index(fields=['booking_date']),
            models.Index(fields=['enrollment_type', 'status']),
        ]

    def save(self, *args, **kwargs):
        # If this is a new booking and no group ID exists, generate one
        if not self.pk and not self.booking_group_id:
            self.booking_group_id = uuid.uuid4()
        super().save(*args, **kwargs)

    def generate_series_bookings(self):
        """
        Generate related bookings for recurring or course enrollments
        """
        from .schedule_utils import generate_recurring_dates
        
        bookings = []
        
        if self.enrollment_type == 'recurring':
            dates = generate_recurring_dates(
                timezone.now().date(),
                self.current_period_end,
                self.recurrence_pattern,
                self.sessions_per_week
            )
        elif self.enrollment_type == 'course':
            dates = generate_recurring_dates(
                self.course_start_date,
                self.course_end_date,
                self.recurrence_pattern,
                self.sessions_per_week
            )
        else:
            return []

        # Find or create schedule instances for these dates
        for date in dates:
            if date == self.schedule_instance.date:
                continue  # Skip the original booking date
            
            schedule = self.schedule_instance.schedule
            instance, _ = ScheduleInstance.objects.get_or_create(
                schedule=schedule,
                date=date,
                defaults={
                    'time': self.schedule_instance.time,
                    'price': self.schedule_instance.price,
                    'max_participants': self.schedule_instance.max_participants,
                    'status': 'scheduled'
                }
            )
            
            bookings.append(Booking(
                schedule_instance=instance,
                student=self.student,
                booking_group_id=self.booking_group_id,
                enrollment_type=self.enrollment_type,
                participants=self.participants,
                amount_paid=instance.price * self.participants,
                status='confirmed',
                payment_status='paid',
                # Copy relevant fields from original booking
                course_start_date=self.course_start_date,
                course_end_date=self.course_end_date,
                total_sessions=self.total_sessions,
                sessions_per_week=self.sessions_per_week,
                recurrence_pattern=self.recurrence_pattern,
                current_period_end=self.current_period_end
            ))

        if bookings:
            Booking.objects.bulk_create(bookings)
        
        return bookings

    def validate_availability(self):
        """Ensure there are enough spots available"""
        instance = self.schedule_instance
        if instance.status != 'scheduled':
            raise ValidationError('This class instance is not available for booking')
            
        if instance.date < timezone.now().date():
            raise ValidationError('Cannot book past class instances')
            
        if not instance.can_accommodate(self.participants):
            raise ValidationError(f'Only {instance.available_spots} spots remaining')

    def renew_recurring(self):
        """Generate next month's bookings for recurring enrollment"""
        if self.enrollment_type != 'recurring':
            raise ValidationError("Can only renew recurring bookings")

        from .schedule_utils import generate_recurring_dates
        
        next_start = self.current_period_end + timedelta(days=1)
        next_end = next_start + timedelta(days=30)
        
        dates = generate_recurring_dates(
            next_start,
            next_end,
            self.recurrence_pattern,
            self.sessions_per_week
        )

        # Create next month's bookings
        bookings = []
        schedule = self.schedule_instance.schedule
        
        for date in dates:
            instance, _ = ScheduleInstance.objects.get_or_create(
                schedule=schedule,
                date=date,
                defaults={
                    'time': self.schedule_instance.time,
                    'price': self.schedule_instance.price,
                    'max_participants': self.schedule_instance.max_participants,
                    'status': 'scheduled'
                }
            )
            
            bookings.append(Booking(
                schedule_instance=instance,
                student=self.student,
                participants=self.participants,
                enrollment_type='recurring',
                recurrence_pattern=self.recurrence_pattern,
                sessions_per_week=self.sessions_per_week,
                current_period_end=next_end,
                status='confirmed',
                amount_paid=instance.price * self.participants,
                payment_status='paid'
            ))

        if bookings:
            Booking.objects.bulk_create(bookings)
            self.current_period_end = next_end
            self.save()

class StudentEnrollment(models.Model):
    """Tracks student enrollment in classes"""
    student = models.ForeignKey('Student', on_delete=models.CASCADE, related_name='enrollments')
    class_option = models.ForeignKey('ClassOption', on_delete=models.CASCADE, related_name='enrollments')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('dropped', 'Dropped'),
        ('pending', 'Pending')
    ], default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'student_enrollments'
        indexes = [
            models.Index(fields=['student', 'status']),
            models.Index(fields=['class_option', 'status']),
        ]