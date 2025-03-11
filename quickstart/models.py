from datetime import timedelta
from django.db import transaction
from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser, Permission
from django.contrib.contenttypes.models import ContentType
from storages.backends.s3boto3 import S3Boto3Storage
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.db.models import Count, Case, When, DecimalField
from decimal import Decimal
from django.utils import timezone
import jsonfield
import uuid

class PermissionGroup(models.Model):
    """Group related permissions together for better organization in the UI"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['sort_order', 'name']
        db_table = 'permission_groups'

class EnhancedPermission(models.Model):
    """Extends the Django Permission system with additional metadata"""
    permission = models.OneToOneField(Permission, on_delete=models.CASCADE, related_name='enhanced')
    group = models.ForeignKey(PermissionGroup, on_delete=models.SET_NULL, null=True, related_name='permissions')
    description = models.TextField(blank=True)
    is_sensitive = models.BooleanField(default=False)
    requires_mfa = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.permission.name} ({self.group.name if self.group else 'Ungrouped'})"
    
    class Meta:
        db_table = 'enhanced_permissions'

class VerificationRequest(models.Model):
    """Stores verification requests for business owners and instructors"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='verification_requests')
    business = models.ForeignKey('BusinessInfo', on_delete=models.CASCADE, related_name='verification_requests', null=True, blank=True)
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, 
                                    null=True, blank=True, related_name='reviewed_verifications')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    
    def __str__(self):
        return f"Verification Request for {self.user.email} ({self.status})"
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Sync with the business model if status changed
        if self.business and hasattr(self, '_original_status') and self._original_status != self.status:
            self.business.verificationStatus = self.status
            self.business.save(update_fields=['verificationStatus'])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track the original status for change detection
        if self.id:
            self._original_status = self.status
    
    class Meta:
        db_table = 'verification_requests'
        ordering = ['-submitted_at']

class VerificationDocument(models.Model):
    """Stores documents submitted as part of a verification request"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification_request = models.ForeignKey(VerificationRequest, on_delete=models.CASCADE, related_name='documents')
    
    DOCUMENT_TYPES = [
        ('business_license', 'Business License'),
        ('id_verification', 'ID Verification'),
        ('certification', 'Professional Certification'),
        ('insurance', 'Insurance Documentation'),
        ('address_proof', 'Proof of Address'),
        ('other', 'Other Document')
    ]
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)
    
    file = models.FileField(upload_to='verification_documents/')
    filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=50)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.get_document_type_display()} for {self.verification_request.user.email}"
    
    class Meta:
        db_table = 'verification_documents'

class AuditLog(models.Model):
    """Comprehensive audit log for user actions"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Who performed the action
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        related_name='audit_logs'
    )
    user_email = models.EmailField()  # Store separately in case user is deleted
    
    # What was done
    ACTION_CHOICES = [
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('user_create', 'User Created'),
        ('user_update', 'User Updated'),
        ('user_delete', 'User Deleted'),
        ('role_change', 'Role Changed'),
        ('permission_change', 'Permission Changed'),
        ('account_lock', 'Account Locked'),
        ('account_unlock', 'Account Unlocked'),
        ('password_reset', 'Password Reset'),
        ('failed_login', 'Failed Login'),
        ('verification_submit', 'Verification Submitted'),
        ('verification_approve', 'Verification Approved'),
        ('verification_reject', 'Verification Rejected'),
        ('system_setting_change', 'System Setting Changed'),
        ('data_export', 'Data Exported')
    ]
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    
    # Details of the action
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    details = models.TextField(blank=True)
    
    # Target of the action (if applicable)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='targeted_logs'
    )
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    
    # Additional metadata
    metadata = jsonfield.JSONField(default=dict, blank=True)
    
    def __str__(self):
        return f"{self.get_action_display()} by {self.user_email} at {self.timestamp}"
    
    class Meta:
        db_table = 'audit_logs'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['action']),
            models.Index(fields=['timestamp']),
            models.Index(fields=['target_user']),
            # Add these new indexes for better performance
            models.Index(fields=['user_email']),
            models.Index(fields=['ip_address']),
            # Compound index for common filter combinations
            models.Index(fields=['action', 'timestamp']),
            models.Index(fields=['user', 'action']),
        ]

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True)
    is_system = models.BooleanField(default=False, help_text="System roles cannot be deleted")
    is_default = models.BooleanField(default=False, help_text="Default role for new users")
    description = models.TextField(blank=True)
    color = models.CharField(max_length=20, default="#64748b", help_text="HEX color code for this role")
    hierarchy_level = models.IntegerField(default=0, help_text="Higher number = higher privileges")
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

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
    
    @classmethod
    def annotate_metrics(cls, queryset):
        """Add all required metrics in a single annotation"""
        return queryset.annotate(
            active_classes=Count(
                'bookings',
                filter=models.Q(bookings__status='confirmed')
            ),
            total_classes_taken=Count(
                'bookings',
                filter=models.Q(bookings__status='completed')
            ),
            average_attendance=Case(
                When(
                    total_finished_bookings__gt=0,
                    then=100.0 * models.F('completed_bookings_count') / 
                         models.F('total_finished_bookings')
                ),
                default=0,
                output_field=DecimalField(max_digits=5, decimal_places=2)
            )
        )
    
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
    featured = models.BooleanField(default=False)
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
    last_booking_date = models.DateTimeField(null=True, blank=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.businessName

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
            models.Index(fields=['featured']),
        ]

class StudentNote(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='business_notes')
    business = models.ForeignKey(BusinessInfo, on_delete=models.CASCADE, related_name='user_notes')
    author = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='authored_notes')
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.user} by {self.author} ({self.business.businessName})"
    
    class Meta:
        db_table = 'student_notes'
        indexes = [
            models.Index(fields=['user', 'business']),
        ]

class ClassImage(models.Model):
    imageId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='class_images/', storage=S3Boto3Storage())
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'class_images'


class ClassCategory(models.Model):
    """Class category model"""
    name = models.CharField(max_length=100)
    key = models.CharField(max_length=100, unique=True)
    color = models.CharField(max_length=20, default="#3b82f6")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        db_table = 'class_categories'
        verbose_name_plural = 'Class Categories'

class ClassSubcategory(models.Model):
    """Class subcategory model"""
    category = models.ForeignKey(ClassCategory, on_delete=models.CASCADE, related_name='subcategories')
    name = models.CharField(max_length=100)
    key = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return f"{self.name} ({self.category.name})"
    
    class Meta:
        db_table = 'class_subcategories'
        unique_together = ['category', 'key']

class ClassesMain(models.Model):
    classId = models.AutoField(primary_key=True)
    businessId = models.ForeignKey('BusinessInfo', on_delete=models.CASCADE)
    
    # Basic Info Fields
    title = models.CharField(max_length=100)
    description = models.TextField(max_length=2000)
    features = models.JSONField(default=list)  # Store as JSON array
    category = models.ForeignKey(ClassCategory, on_delete=models.SET_NULL, null=True, related_name='classes')
    subcategory = models.ForeignKey(ClassSubcategory, on_delete=models.SET_NULL, null=True, blank=True, related_name='classes')
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('suspended', 'Suspended')
    ]
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='active'
    )

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
        indexes = [
            models.Index(fields=['coordinates']),
            models.Index(fields=['location']),
            models.Index(fields=['businessId']),
            models.Index(fields=['status'])
        ]

class Favorites(models.Model):
    favoriteId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='favorites')
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'favorites'

class ClassOption(models.Model):
    optionId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', on_delete=models.CASCADE, related_name='options')

    # Basic Info
    title = models.CharField(max_length=100)
    description = models.TextField(null=True, blank=True)
    
    BOOKING_TYPES = [
        ('Single Session', 'Single Session'),
        ('Full Course', 'Full Course')
    ]
    booking_type = models.CharField(
        max_length=20,
        choices=BOOKING_TYPES,
        default='Single Session'
    )

    image = models.ImageField(
        upload_to='class_options/', 
        storage=S3Boto3Storage(),
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
    PRICE_TYPES = [
        ('per_session', 'Per Session'),
        ('full_course', 'Full Course')
    ]
    price_type = models.CharField(
        max_length=20, 
        choices=PRICE_TYPES,
        default='per_session'
    )
    
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
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    def get_image_url(self):
        if self.image:
            return self.image.url
        return None
    
    class Meta:
        db_table = 'class_options'
        indexes = [
            models.Index(fields=['classId'])
        ]

class Schedule(models.Model):
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
        ],
        null=True,
        blank=True
    )
    time = models.TimeField()
    duration = models.IntegerField(default=60)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    maxParticipants = models.IntegerField(validators=[MinValueValidator(1)])

    # Only used for courses
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    
    # Only used for single sessions
    date = models.DateField(null=True, blank=True)
    
    is_active = models.BooleanField(default=True)
    allow_late_enrollment = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        
        # For single sessions, derive day from date
        if self.option.booking_type != 'Full Course' and self.date:
            if self.date:
                self.day = self.date.strftime('%a')
            
        super().save(*args, **kwargs)
        
        if is_new:
            if self.option.booking_type == 'Full Course':
                self.generate_course_instances()
            else:
                # For single sessions, create just one instance
                if self.date:
                    ScheduleInstance.objects.create(
                        schedule=self,
                        date=self.date,
                        time=self.time,
                        duration=self.duration,
                        price=self.price,
                        max_participants=self.maxParticipants,
                        status='scheduled'
                    )

    def delete(self, *args, **kwargs):
        """
        Override delete method to mark schedules and instances as inactive
        instead of actually deleting them to preserve booking relationships.
        """
        force_delete = kwargs.pop('force_delete', False)
        
        # If force_delete is True, perform actual deletion
        if force_delete:
            return super().delete(*args, **kwargs)
        
        with transaction.atomic():
            # Get all related schedule instances
            instance_ids = list(self.instances.values_list('id', flat=True))
            
            if instance_ids:
                # Cancel all related bookings that are still confirmed
                Booking.objects.filter(
                    schedule_instance_id__in=instance_ids,
                    status='confirmed'
                ).update(
                    status='cancelled',
                    cancelled_at=timezone.now(),
                    cancellation_reason='Schedule was removed by the instructor'
                )
                
                # Mark all schedule instances as cancelled
                self.instances.filter(
                    status='scheduled'
                ).update(
                    status='cancelled',
                    cancellation_reason='Schedule was deactivated by the instructor'
                )
                
            # Mark the schedule as inactive rather than deleting it
            self.is_active = False
            self.save(update_fields=['is_active'])
            
            return (0, {})

    # Rest of the methods remain unchanged
    def generate_course_instances(self):
        """Generate all instances for a course between start and end date"""
        if self.option.booking_type != 'Full Course':
            return []
            
        if not (self.start_date and self.end_date and self.day):
            raise ValidationError("Start date, end date, and day required for course schedules")

        day_to_number = {
            'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 
            'Fri': 4, 'Sat': 5, 'Sun': 6
        }
        
        target_weekday = day_to_number[self.day]
        current_date = self.start_date
        
        # Move to first occurrence of target weekday if needed
        while current_date.weekday() != target_weekday:
            current_date += timedelta(days=1)
        
        instances = []
        while current_date <= self.end_date:
            instance = ScheduleInstance(
                schedule=self,
                date=current_date,
                time=self.time,
                price=self.price,
                max_participants=self.maxParticipants,
                duration=self.duration
            )
            instances.append(instance)
            current_date += timedelta(weeks=1)
            
        return ScheduleInstance.objects.bulk_create(instances)

    def clean(self):
        if self.option.booking_type == 'Full Course':
            if not all([self.start_date, self.end_date, self.day]):
                raise ValidationError({
                    'course_dates': 'Start date, end date, and day required for courses'
                })
            if self.start_date and self.end_date and self.start_date >= self.end_date:
                raise ValidationError({
                    'course_dates': 'End date must be after start date'
                })
            if self.start_date and self.start_date < timezone.now().date():
                raise ValidationError({
                    'start_date': 'Course cannot start in the past'
                })
        else:
            if not self.date:
                raise ValidationError({
                    'date': 'Date is required for single sessions'
                })
            if self.date < timezone.now().date():
                raise ValidationError({
                    'date': 'Session cannot be scheduled in the past'
                })
            
    class Meta:
        db_table = 'schedules'
        ordering = ['day', 'time']
        indexes = [
            models.Index(fields=['option', 'day', 'time']),
            models.Index(fields=['option', 'is_active'])
        ]

class ScheduleInstance(models.Model):
    """Specific occurrence of a schedule"""
    schedule = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='instances')
    date = models.DateField()
    time = models.TimeField()
    duration = models.IntegerField(default=60)
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

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            Booking.objects.filter(
                schedule_instance=self,
                status='confirmed'
            ).update(
                status='cancelled',
                cancelled_at=timezone.now(),
                cancellation_reason='Schedule instance was removed by the instructor'
            )
            
            # Now proceed with deletion
            super().delete(*args, **kwargs)

    @property
    def current_bookings(self):
        return getattr(self, 'booking_count', 0)
    
    @property
    def available_spots(self):
        return self.max_participants - self.current_bookings

    @classmethod
    def get_available_in_range(cls, option_id, start_date, end_date):
        """Get available instances within a date range"""
        return cls.objects.filter(
            schedule__option_id=option_id,
            schedule__is_active=True,
            date__range=(start_date, end_date),
            status='scheduled'
        ).select_related('schedule').order_by('date', 'time')

    def can_accommodate(self, requested_participants):
        """Check if instance can accommodate requested number of participants"""
        return self.available_spots >= requested_participants

    class Meta:
        db_table = 'schedule_instances'
        unique_together = ['schedule', 'date']
        indexes = [
            models.Index(fields=['schedule', 'date']),
            models.Index(fields=['schedule', 'status']),
            models.Index(fields=['date', 'time']),
            models.Index(fields=['schedule', 'date', 'status']),
            
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

class Booking(models.Model):
    id = models.AutoField(primary_key=True)
    booking_group_id = models.UUIDField(null=True, blank=True)
    schedule_instance = models.ForeignKey(ScheduleInstance, on_delete=models.CASCADE, related_name='bookings')
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, related_name='bookings')
    
    enrollment_type = models.CharField(
        max_length=20, 
        choices=[
            ('Single Session', 'Single Session'),
            ('Full Course', 'Full Course'),
        ], 
        default='Single Session'
    )
    
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'), 
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed')
    ], default='pending')
    
    booking_date = models.DateTimeField(auto_now_add=True)
    participants = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(4)])
    notes = models.TextField(blank=True)
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
            models.Index(fields=['user', 'status']),  # Changed from student to user
            models.Index(fields=['booking_date']),
            models.Index(fields=['enrollment_type', 'status']),
            models.Index(fields=['status']),
            models.Index(fields=['booking_group_id']),
        ]

class Payment(models.Model):
    """Stores detailed payment information"""
    id = models.AutoField(primary_key=True)
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name='payments')
    
    # Stripe specific fields
    stripe_payment_intent_id = models.CharField(max_length=255, unique=True)
    stripe_charge_id = models.CharField(max_length=255, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    service_fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    currency = models.CharField(max_length=3, default='USD')
    
    # Status tracking
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
        ('partially_refunded', 'Partially Refunded')
    ], default='pending')
    
    # Payment method details
    payment_method_type = models.CharField(max_length=20, default='card')
    card_brand = models.CharField(max_length=20, null=True, blank=True)
    card_last4 = models.CharField(max_length=4, null=True, blank=True)
    card_exp_month = models.IntegerField(null=True, blank=True)
    card_exp_year = models.IntegerField(null=True, blank=True)
    
    # Refund tracking
    refunded_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refund_reason = models.TextField(blank=True)
    refund_date = models.DateTimeField(null=True, blank=True)
    
    # Receipt and metadata
    receipt_url = models.URLField(max_length=500, null=True, blank=True)
    receipt_number = models.CharField(max_length=100, null=True, blank=True)
    failure_message = models.TextField(blank=True)
    billing_details = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Payment {self.id} - {self.stripe_payment_intent_id} ({self.status})"
    
    @property
    def formatted_status(self):
        return self.status.replace('_', ' ').title()
    
    @property
    def is_refundable(self):
        return self.status == 'succeeded' and self.refunded_amount < self.amount
    
    @property
    def available_refund_amount(self):
        return self.amount - self.refunded_amount
    
    class Meta:
        db_table = 'payments'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['stripe_payment_intent_id']),
            models.Index(fields=['stripe_charge_id']),
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
        ]

class Reviews(models.Model):
    reviewId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='reviews')
    businessId = models.ForeignKey(BusinessInfo, models.CASCADE, db_column='businessId')
    classId = models.ForeignKey(ClassesMain, models.CASCADE, db_column='classId', related_name='reviews')
    booking = models.OneToOneField(
        Booking, 
        on_delete=models.SET_NULL, 
        related_name='review', 
        null=True, 
        blank=True
    )
    rating = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField()
    image = models.ImageField(
        upload_to='review_images/',
        storage=S3Boto3Storage(),
        null=True,
        blank=True
    )
    status = models.CharField(
        max_length=20, 
        choices=[
            ('approved', 'Approved'),
            ('under_review', 'Under Review'),
            ('hidden', 'Hidden')
        ],
        default='approved'
    )
    reported = models.BooleanField(default=False)
    report_reason = models.TextField(blank=True)
    business_response = models.TextField(blank=True)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'reviews'
        indexes = [
            models.Index(fields=['classId']),
            models.Index(fields=['booking']),
        ]

class ChatSession(models.Model):
    userId = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='chat_session')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'chat_sessions'

class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE)
    content = models.TextField()
    is_user = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'chat_messages'
        ordering = ['created_at']

class SupportTicket(models.Model):
    ticket_id = models.AutoField(primary_key=True)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='support_tickets')
    chat_session = models.ForeignKey(ChatSession, on_delete=models.SET_NULL, null=True, related_name='tickets')
    
    # Ticket categorization
    category = models.CharField(max_length=50, choices=[
        ('account', 'Account Issues'),
        ('booking', 'Booking Problems'),
        ('payment', 'Payment Issues'),
        ('technical', 'Technical Support'),
        ('feature', 'Feature Request'),
        ('other', 'Other')
    ])
    
    subject = models.CharField(max_length=100)
    description = models.TextField()
    
    # Status tracking
    status = models.CharField(max_length=20, choices=[
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed')
    ], default='open')
    
    priority = models.CharField(max_length=20, choices=[
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('urgent', 'Urgent')
    ], default='medium')
    
    # Timestamps and management
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    assigned_to = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='assigned_tickets')
    resolution_notes = models.TextField(blank=True)
    
    class Meta:
        db_table = 'support_tickets'
        ordering = ['-created_at']
        
    def __str__(self):
        return f"Ticket #{self.ticket_id}: {self.subject}"