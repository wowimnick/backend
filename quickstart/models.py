from asyncio.log import logger
from datetime import timedelta
import math
import random
import string
import pytz
from django.db import transaction
from django.conf import settings
from django.db import models
from django.contrib.auth.models import AbstractUser, Permission
from django.contrib.contenttypes.models import ContentType
from django.utils.text import slugify as django_slugify
from storages.backends.s3boto3 import S3Boto3Storage
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.postgres.search import SearchVectorField
from django.contrib.postgres.search import SearchVector
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db.models import Count, Case, When, DecimalField, Sum, JSONField, Avg, Value
from django.db.models.functions import Coalesce
from django.contrib.contenttypes.fields import GenericForeignKey
from decimal import Decimal
from django.utils import timezone
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
import jsonfield
import uuid

COMMON_TIMEZONE_CHOICES = [(tz, tz.replace("_", " ")) for tz in pytz.common_timezones]


class PermissionGroup(models.Model):
    """Group related permissions together for better organization in the UI"""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["sort_order", "name"]
        db_table = "permission_groups"


class EnhancedPermission(models.Model):
    """Extends the Django Permission system with additional metadata"""

    permission = models.OneToOneField(
        Permission, on_delete=models.CASCADE, related_name="enhanced"
    )
    group = models.ForeignKey(
        PermissionGroup,
        on_delete=models.SET_NULL,
        null=True,
        related_name="permissions",
    )
    description = models.TextField(blank=True)
    is_sensitive = models.BooleanField(default=False)
    requires_mfa = models.BooleanField(default=False)

    def __str__(self):
        return (
            f"{self.permission.name} ({self.group.name if self.group else 'Ungrouped'})"
        )

    class Meta:
        db_table = "enhanced_permissions"


class VerificationRequest(models.Model):
    """Stores verification requests for business owners and instructors"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="verification_requests",
    )
    business = models.ForeignKey(
        "BusinessInfo",
        on_delete=models.CASCADE,
        related_name="verification_requests",
        null=True,
        blank=True,
    )

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_verifications",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Verification Request for {self.user.email} ({self.status})"

    def save(self, *args, **kwargs):
        # Determine if status is changing. _original_status is set in __init__ for existing objects.
        # For new objects, we don't need to compare as there's no previous status to sync.
        status_changed = (
            hasattr(self, "_original_status")
            and self._original_status is not None
            and self._original_status != self.status
        )

        super().save(*args, **kwargs)  # Save the VerificationRequest first

        if self.business and status_changed:
            self.business.verificationStatus = self.status
            self.business.save(update_fields=["verificationStatus"])

        # Update _original_status after save to reflect the new current state for subsequent saves
        self._original_status = self.status

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.pk:
            self._original_status = self.status
        else:
            self._original_status = None

    class Meta:
        db_table = "verification_requests"
        ordering = ["-submitted_at"]
        permissions = [
            ("view_all_verificationrequests", "Can view all verification requests"),
            (
                "process_verificationrequest",
                "Can approve or reject verification requests",
            ),
        ]


class VerificationDocument(models.Model):
    """Stores documents submitted as part of a verification request"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    verification_request = models.ForeignKey(
        VerificationRequest, on_delete=models.CASCADE, related_name="documents"
    )

    DOCUMENT_TYPES = [
        ("business_license", "Business License"),
        ("id_verification", "ID Verification"),
        ("certification", "Professional Certification"),
        ("insurance", "Insurance Documentation"),
        ("address_proof", "Proof of Address"),
        ("other", "Other Document"),
    ]
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES)

    file = models.FileField(upload_to="verification_documents/")
    filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=50)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_document_type_display()} for {self.verification_request.user.email}"

    class Meta:
        db_table = "verification_documents"


class AuditLog(models.Model):
    """Comprehensive audit log for user actions"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Who performed the action
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
    )
    user_email = models.EmailField()  # Store separately in case user is deleted

    # What was done
    ACTION_CHOICES = [
        ("login", "Login"),
        ("logout", "Logout"),
        ("user_create", "User Created"),
        ("user_update", "User Updated"),
        ("user_delete", "User Deleted"),
        ("role_change", "Role Changed"),
        ("permission_change", "Permission Changed"),
        ("account_lock", "Account Locked"),
        ("account_unlock", "Account Unlocked"),
        ("password_reset", "Password Reset"),
        ("failed_login", "Failed Login"),
        ("verification_submit", "Verification Submitted"),
        ("verification_approve", "Verification Approved"),
        ("verification_reject", "Verification Rejected"),
        ("system_setting_change", "System Setting Changed"),
        ("data_export", "Data Exported"),
        ("business_update", "Business Updated"),
        ("business_delete", "Business Deleted"),
        ("business_delete_failed", "Business Delete Failed"),
        ("business_activate", "Business Activated"),
        ("business_deactivate", "Business Deactivated"),
        ("business_feature", "Business Featured"),
        ("business_unfeature", "Business Unfeatured"),
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
        related_name="targeted_logs",
    )
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=100, blank=True)

    # Additional metadata
    metadata = jsonfield.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.get_action_display()} by {self.user_email} at {self.timestamp}"

    class Meta:
        db_table = "audit_logs"
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["action"]),
            models.Index(fields=["timestamp"]),
            models.Index(fields=["target_user"]),
            # Add these new indexes for better performance
            models.Index(fields=["user_email"]),
            models.Index(fields=["ip_address"]),
            # Compound index for common filter combinations
            models.Index(fields=["action", "timestamp"]),
            models.Index(fields=["user", "action"]),
        ]


class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True)
    is_system = models.BooleanField(
        default=False, help_text="System roles cannot be deleted"
    )
    is_default = models.BooleanField(
        default=False, help_text="Default role for new users"
    )
    description = models.TextField(blank=True)
    color = models.CharField(
        max_length=20, default="#64748b", help_text="HEX color code for this role"
    )
    hierarchy_level = models.IntegerField(
        default=0, help_text="Higher number = higher privileges"
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = "roles"


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
    avatar = models.ImageField(
        upload_to="originals/avatars/",
        max_length=255,
        null=True,
        blank=True,
    )
    createdAt = models.DateTimeField(auto_now_add=True)
    role = models.ForeignKey(
        "Role", on_delete=models.SET_NULL, null=True, blank=True
    )  # Assuming Role model is defined
    favorited = models.ManyToManyField(
        "ClassesMain", through="Favorites", related_name="favorited_by", blank=True
    )
    user_timezone = models.CharField(
        max_length=50,
        choices=COMMON_TIMEZONE_CHOICES,
        default="UTC",  # Sensible default
        blank=True,  # Allow blank if you want to prompt user or guess later
        help_text="User's preferred IANA timezone for displaying dates/times.",
    )
    is_unsubscribed = models.BooleanField(
        default=False, help_text="User has opted out of marketing emails."
    )
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = [
        "username"
    ]  # Keep username for AbstractUser compatibility, though email is login

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
                "bookings", filter=models.Q(bookings__status="confirmed")
            ),
            total_classes_taken=Count(
                "bookings", filter=models.Q(bookings__status="completed")
            ),
            average_attendance=Case(
                When(
                    total_finished_bookings__gt=0,  # Assuming this field is annotated separately
                    then=100.0
                    * models.F(
                        "completed_bookings_count"
                    )  # Assuming this field is annotated separately
                    / models.F("total_finished_bookings"),
                ),
                default=0,
                output_field=DecimalField(max_digits=5, decimal_places=2),
            ),
        )

    @property
    def active_classes(self):
        """Count of currently confirmed bookings"""
        return self.bookings.filter(status="confirmed").count()

    @property
    def total_classes_taken(self):
        """Count of completed bookings"""
        return self.bookings.filter(status="completed").count()

    @property
    def average_attendance(self):
        """Calculate attendance rate from completed vs total finished bookings"""
        total_finished = self.bookings.filter(
            status__in=["completed", "cancelled"]
        ).count()
        if total_finished == 0:
            return Decimal("0.00")
        completed = self.bookings.filter(status="completed").count()
        return Decimal(str(round((completed / total_finished) * 100, 2)))

    class Meta:
        db_table = "users"
        permissions = [
            # --- Platform Admin Permissions (Keep these as they are) ---
            ("change_user_role", "Can change the role assigned to any user"),
            ("lock_user", "Can lock/unlock any user account"),
            ("reset_user_password", "Can initiate password reset for any user"),
            ("view_user_metrics", "Can view user management metrics"),
            ("access_user_admin", "Can access the user administration section"),
            ("view_system_metrics", "Can view real-time system performance metrics"),
            (
                "access_admin_dashboard",
                "Can access the main platform administration dashboard",
            ),
            ("access_blog_admin", "Can access the Blog Management section"),
            # --- User/Student Permissions (Keep these as they are) ---
            ("reply_own_support_ticket", "Can reply to own support tickets"),
            (
                "cancel_own_booking",
                "Can cancel their own booking",
            ),  # Moved from Booking model for clarity
            (
                "add_supportticket",
                "Can create new support tickets",
            ),  # Added for user creation
        ]


REFUND_POLICY_CHOICES = [
    ("full", "Full refund if cancelled within policy"),
    ("partial", "Partial refund (details TBD/in description)"),
    ("credit", "Account credit only"),
    ("none", "No refunds"),
]

CANCELLATION_POLICY_CHOICES = [
    ("flexible", "Flexible (up to 1 hour before)"),
    ("24h", "24 Hours Notice"),
    ("48h", "48 Hours Notice"),
    ("72h", "72 Hours Notice"),
    ("strict", "Strict (Non-refundable)"),
]

STRIPE_STATUS_CHOICES = [
    ("unlinked", "Unlinked"),
    ("pending", "Pending Verification"),
    ("active", "Connected & Active"),
    ("restricted", "Account Restricted"),
    ("incomplete", "Incomplete Setup"),
]


class BusinessInfo(models.Model):
    # --- Core Fields ---
    businessId = models.AutoField(primary_key=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_businesses",
    )
    businessName = models.CharField(max_length=100)
    CONTACT_PRIVACY_CHOICES = [
        ("on_booking", "Show After Booking"),
        ("public", "Show Publicly"),
        ("public_with_chat", "Show Publicly & Allow Chat"),
    ]
    contact_privacy = models.CharField(
        max_length=20,
        choices=CONTACT_PRIVACY_CHOICES,
        default="on_booking",
        help_text="Choose who can see your direct contact information.",
    )
    businessType = models.CharField(
        max_length=50,
        choices=[
            ("individual", "Individual Teacher"),
            ("school", "School"),
            ("studio", "Studio"),
            ("academy", "Academy"),
            ("center", "Learning Center"),
        ],
    )
    businessDescription = models.TextField(max_length=750)
    businessImage = models.ImageField(
        upload_to="originals/business_images/",
        blank=True,
        null=True,
        max_length=255,
    )
    featured = models.BooleanField(default=False)
    isActive = models.BooleanField(default=False)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    # --- Contact & Website ---
    studentContactPhone = models.CharField(max_length=100)
    studentContactEmail = models.EmailField()
    website = models.URLField(max_length=255, blank=True, null=True)  # ADDED
    preferredContact = models.CharField(
        max_length=20,
        choices=[
            ("email", "Email"),
            ("phone", "Phone"),
            ("both", "Both Email and Phone"),
        ],
    )

    # --- Location ---
    businessAddress = models.CharField(max_length=255)
    businessCity = models.CharField(max_length=100)
    businessState = models.CharField(max_length=100)
    businessZipCode = models.CharField(max_length=20)
    latitude = models.DecimalField(
        max_digits=10, decimal_places=8, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=11, decimal_places=8, null=True, blank=True
    )
    showExactLocation = models.BooleanField(default=True)
    business_timezone = models.CharField(  # ADDED
        max_length=50,
        choices=COMMON_TIMEZONE_CHOICES,
        default="America/Toronto",
        blank=False,  # Make it required during registration
        help_text="Primary IANA timezone for this business's operations.",
    )

    # --- Simplified Booking Settings ---
    openingTime = models.TimeField()
    closingTime = models.TimeField()
    # refundPolicy = models.CharField(...) # REMOVED

    # Notification fields
    newBookingNotification = models.BooleanField(default=True)
    cancellationNotification = models.BooleanField(default=True)
    reminderNotification = models.BooleanField(default=True)
    smsNotifications = models.BooleanField(default=False)

    # --- Stripe Connect Fields ---
    currency = models.CharField(
        max_length=3,
        default="CAD",  # Set a sensible default for your primary market
        help_text="3-letter ISO currency code, e.g., CAD, USD.",
    )
    stripe_account_id = models.CharField(
        max_length=255, blank=True, null=True, unique=True, db_index=True
    )
    stripe_account_status = models.CharField(
        max_length=30,
        choices=STRIPE_STATUS_CHOICES,
        blank=True,
        null=True,
        db_index=True,
    )

    managers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="managed_businesses", blank=True
    )
    liabilityWaiver = models.BooleanField(default=False)

    # --- Class/Category Information ---
    classFormats = models.JSONField(default=list, blank=True)
    skillLevels = models.JSONField(default=list, blank=True)
    ageGroups = models.JSONField(default=list, blank=True)

    # --- Verification & Agreements ---
    verificationDocument = models.FileField(
        upload_to="verification_documents/",
        blank=True,
        null=True,
    )
    verificationStatus = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("verified", "Verified"),
            ("rejected", "Rejected"),
        ],
        default="pending",
        db_index=True,
    )
    termsAccepted = models.BooleanField(default=False)
    privacyAccepted = models.BooleanField(default=False)

    # --- Additional Useful Fields ---
    social_media_links = models.JSONField(
        default=dict,
        blank=True,
        help_text="e.g. {'facebook': 'url', 'instagram': 'url'}",
    )  # ADDED
    tags_keywords = models.JSONField(
        default=list, blank=True, help_text="List of keywords for searchability"
    )  # ADDED
    founding_year = models.PositiveIntegerField(
        null=True, blank=True, help_text="Year the business was founded"
    )  # ADDED

    # --- Cached Aggregates ---
    total_reviews_count = models.IntegerField(
        default=0,
        editable=False,
        help_text="Cached count of approved reviews for this business",
    )
    average_rating = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        default=Decimal("0.0"),
        editable=False,
        help_text="Cached average rating from approved reviews for this business",
    )

    last_booking_date = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.businessName

    def update_review_aggregates(self):
        # (Keep your existing implementation of this method)
        approved_reviews_qs = Reviews.objects.filter(
            classId__businessId=self,
            status="approved",
        )
        new_count = approved_reviews_qs.count()
        new_avg_rating_data = approved_reviews_qs.aggregate(avg=Avg("rating"))
        new_avg_rating = new_avg_rating_data["avg"] or Decimal("0.0")
        new_avg_rating_rounded = Decimal(str(round(new_avg_rating, 1)))

        needs_update = False
        if self.total_reviews_count != new_count:
            self.total_reviews_count = new_count
            needs_update = True
        if self.average_rating != new_avg_rating_rounded:
            self.average_rating = new_avg_rating_rounded
            needs_update = True

        if needs_update:
            self.save(update_fields=["total_reviews_count", "average_rating"])
            logger.info(
                f"Updated review aggregates for Business {self.businessId}: Count={self.total_reviews_count}, AvgRating={self.average_rating}"
            )

    class Meta:
        db_table = "business_info"
        verbose_name_plural = "Business Information"
        indexes = [
            models.Index(fields=["businessType"]),
            models.Index(fields=["isActive"]),
            models.Index(fields=["featured"]),
            models.Index(fields=["verificationStatus"]),
            models.Index(fields=["stripe_account_id"]),
            models.Index(fields=["stripe_account_status"]),
            models.Index(fields=["total_reviews_count"]),
            models.Index(fields=["average_rating"]),
        ]
        permissions = [
            (
                "toggle_business_feature",
                "Can toggle the featured status for any business",
            ),
            (
                "view_business_metrics",
                "Can view aggregated business management statistics",
            ),
            ("export_business_data", "Can export business data as CSV"),
            (
                "send_business_announcements",
                "Can send platform announcements to businesses",
            ),
            ("access_business_admin", "Can access the Business Administration section"),
            (
                "manage_own_classes",
                "Can create/edit classes, options, schedules for own business",
            ),
            (
                "manage_own_schedule_instances",
                "Can manage instances (attendance, cancel) for own classes",
            ),
            ("view_own_business_bookings", "Can view bookings for own business"),
            ("manage_own_business_profile", "Can edit own business profile details"),
            (
                "manage_business_staff",
                "Can manage staff (instructors, managers) for own business",
            ),
            (
                "access_business_dashboard",
                "Can access the dashboard for managing their own business",
            ),
            (
                "view_business_revenue_analytics",
                "Can view revenue analytics for own business",
            ),
            (
                "export_business_revenue_data",
                "Can export revenue data for own business",
            ),
            (
                "view_business_students",
                "Can view students associated with own business",
            ),
            (
                "add_studentnote",
                "Can add notes to students associated with own business",
            ),
            (
                "view_studentnote",
                "Can view notes for students associated with own business",
            ),
        ]


class StudentNote(models.Model):
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="business_notes"
    )
    business = models.ForeignKey(
        BusinessInfo, on_delete=models.CASCADE, related_name="user_notes"
    )
    author = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, related_name="authored_notes"
    )
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.user} by {self.author} ({self.business.businessName})"

    class Meta:
        db_table = "student_notes"
        indexes = [
            models.Index(fields=["user", "business"]),
        ]


class ClassImage(models.Model):
    imageId = models.AutoField(primary_key=True)
    classId = models.ForeignKey(
        "ClassesMain", related_name="images", on_delete=models.CASCADE
    )
    image = models.ImageField(
        upload_to="originals/class_images/",
        max_length=255,
        help_text="Image uploaded by the user, e.g., a photo from the class.",
    )
    isCover = models.BooleanField(default=False)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "class_images"


class ClassCategory(models.Model):
    """Class category model"""

    name = models.CharField(max_length=100)
    key = models.SlugField(
        max_length=120,
        unique=True,
        help_text="URL-friendly identifier. Auto-generates from name if not provided.",
    )
    description = models.TextField(
        blank=True, help_text="A short, catchy description for the homepage card."
    )
    image = models.ImageField(
        upload_to="originals/category_images/",
        blank=True,
        null=True,
        help_text="Image displayed on the homepage category card.",
    )
    is_featured = models.BooleanField(
        default=False, db_index=True, help_text="Show this category on the homepage."
    )
    color = models.CharField(max_length=20, default="#3b82f6")
    icon_name = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Name of the Lucide React icon (e.g., 'Music', 'Palette'). See lucide.dev for names.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.key:
            from django.utils.text import slugify

            self.key = slugify(self.name)
        super().save(*args, **kwargs)

    class Meta:
        db_table = "class_categories"
        verbose_name_plural = "Class Categories"
        permissions = [
            ("view_category_stats", "Can view category statistics"),
            ("access_category_admin", "Can access the Category Administration section"),
        ]


class ClassSubcategory(models.Model):
    """Class subcategory model"""

    category = models.ForeignKey(
        ClassCategory, on_delete=models.CASCADE, related_name="subcategories"
    )
    name = models.CharField(max_length=100)
    key = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} ({self.category.name})"

    class Meta:
        db_table = "class_subcategories"
        unique_together = ["category", "key"]


class ClassesMain(models.Model):
    classId = models.AutoField(primary_key=True)
    businessId = models.ForeignKey(
        "BusinessInfo", on_delete=models.CASCADE, related_name="classes"
    )
    slug = models.SlugField(
        max_length=255,
        unique=True,
        blank=False,  # Now required
        help_text="SEO-friendly URL slug. Auto-generated from title and city.",
        db_index=True,
    )
    title = models.CharField(max_length=100)
    description = models.TextField(max_length=2000)
    features = models.JSONField(default=list)
    category = models.ForeignKey(
        ClassCategory,
        on_delete=models.PROTECT,
        null=False,
        related_name="classes_in_category",
    )
    subcategory = models.ForeignKey(
        ClassSubcategory,
        on_delete=models.PROTECT,
        related_name="classes_in_subcategory",
        null=True,
        blank=True,
    )
    STATUS_CHOICES = [
        ("active", "Active"),
        ("inactive", "Inactive"),
        ("suspended", "Suspended"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active")
    location = models.CharField(max_length=255)
    coordinates = models.CharField(max_length=50)
    latitude = models.DecimalField(
        max_digits=10, decimal_places=8, null=True, blank=True, db_index=True
    )
    longitude = models.DecimalField(
        max_digits=11, decimal_places=8, null=True, blank=True, db_index=True
    )
    saltLocation = models.BooleanField(default=False)
    studentContactEmail = models.EmailField(null=True, blank=True)
    studentContactPhone = models.CharField(max_length=20, null=True, blank=True)
    adminContactEmail = models.EmailField(null=True, blank=True)
    adminContactPhone = models.CharField(max_length=20, null=True, blank=True)
    search_vector = SearchVectorField(null=True, editable=False)
    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    def _generate_unique_slug(self):
        """Generates a unique slug from the business city and class title."""
        if self.slug:  # Do not regenerate if a slug already exists
            return

        # Create a base slug from city and title for better SEO
        base_slug = django_slugify(f"{self.businessId.businessCity} {self.title}")

        # If the base slug is empty, fallback to a generic one
        if not base_slug:
            base_slug = "class"

        slug = base_slug
        # Use a transaction to ensure atomic check and creation
        with transaction.atomic():
            # Check for uniqueness and append a suffix if necessary
            while ClassesMain.objects.filter(slug=slug).exists():
                random_suffix = uuid.uuid4().hex[:6]
                slug = f"{base_slug}-{random_suffix}"
        self.slug = slug

    def save(self, *args, **kwargs):
        """Override save to generate a slug if one doesn't exist."""
        if not self.slug:
            self._generate_unique_slug()
        super().save(*args, **kwargs)

    class Meta:
        db_table = "classes"
        indexes = [
            models.Index(fields=["coordinates"]),
            models.Index(fields=["location"]),
            models.Index(fields=["businessId"]),
            models.Index(fields=["status"]),
            models.Index(fields=["slug"]),
            GinIndex(fields=["search_vector"]),
        ]
        permissions = [
            ("change_class_status", "Can change class status"),
            ("view_class_analytics", "View class analytics"),
            ("export_class_data", "Can export class data"),
            ("access_class_admin", "Access Class Admin"),
        ]

    def __str__(self):
        return self.title


# --- Signal Handlers to Update Search Vector for ClassesMain ---
def get_classesmain_search_vector(instance: ClassesMain):
    """Helper function to construct the search vector for a ClassesMain instance."""
    vector_components = [
        SearchVector(Value(instance.title), weight="A", config="english"),
        SearchVector(Value(instance.description), weight="B", config="english"),
    ]
    if instance.businessId:
        vector_components.append(
            SearchVector(
                Value(instance.businessId.businessName), weight="B", config="english"
            )
        )
    if instance.category:
        vector_components.append(
            SearchVector(
                Value(instance.category.name), weight="C", config="pg_catalog.english"
            )
        )
    if instance.subcategory:
        vector_components.append(
            SearchVector(
                Value(instance.subcategory.name),
                weight="D",
                config="pg_catalog.english",
            )
        )

    if not vector_components:
        return SearchVector(Value(""))

    final_vector = vector_components[0]
    for component in vector_components[1:]:
        final_vector += component
    return final_vector


@receiver(post_save, sender=ClassesMain)
def classesmain_post_save_receiver(sender, instance, created, update_fields, **kwargs):
    if kwargs.get("raw", False):
        return  # Skip for fixture loading

    # Determine if vector needs update: new, or relevant fields changed (simplified)
    # A more robust check would compare old values of text fields to new values.
    should_update = created
    if not created and update_fields:
        text_fields = {"title", "description"}  # Fields on ClassesMain itself
        if any(f in update_fields for f in text_fields):
            should_update = True
    elif not created and update_fields is None:  # Full save, assume update needed
        should_update = True

    if should_update:
        new_vector = get_classesmain_search_vector(instance)
        # Update only if vector changed to avoid recursion if a field in vector didn't change
        # This direct comparison might not be perfect.
        if instance.search_vector != new_vector:  # Check if change is needed
            ClassesMain.objects.filter(pk=instance.pk).update(search_vector=new_vector)
            # logger.info(f"Search vector updated for ClassesMain {instance.pk}")


@receiver(post_save, sender="quickstart.ClassOption")
@receiver(post_delete, sender="quickstart.ClassOption")  # Also update on delete
def classoption_change_receiver(sender, instance, **kwargs):
    if hasattr(instance, "classId") and instance.classId:
        class_instance = instance.classId
        new_vector = get_classesmain_search_vector(class_instance)
        if class_instance.search_vector != new_vector:
            ClassesMain.objects.filter(pk=class_instance.pk).update(
                search_vector=new_vector
            )
            # logger.info(f"SV for Class {class_instance.pk} updated due to ClassOption change.")


@receiver(post_save, sender="quickstart.BusinessInfo")
def businessinfo_change_receiver(sender, instance, update_fields, **kwargs):
    if kwargs.get("raw", False):
        return
    if update_fields is None or "businessName" in update_fields:
        # This still iterates, which is not ideal, but wrapping it in a single transaction helps.
        # A true fix requires background tasks (e.g., Celery).
        with transaction.atomic():
            for class_instance in instance.classes.iterator():
                new_vector = get_classesmain_search_vector(class_instance)
                if class_instance.search_vector != new_vector:
                    ClassesMain.objects.filter(pk=class_instance.pk).update(
                        search_vector=new_vector
                    )


@receiver(post_save, sender="quickstart.ClassCategory")
def classcategory_change_receiver(sender, instance, update_fields, **kwargs):
    if kwargs.get("raw", False):
        return
    if update_fields is None or "name" in update_fields:
        with transaction.atomic():
            for class_instance in instance.classes_in_category.iterator():
                new_vector = get_classesmain_search_vector(class_instance)
                if class_instance.search_vector != new_vector:
                    ClassesMain.objects.filter(pk=class_instance.pk).update(
                        search_vector=new_vector
                    )


@receiver(post_save, sender="quickstart.ClassSubcategory")
def classsubcategory_change_receiver(sender, instance, update_fields, **kwargs):
    if kwargs.get("raw", False):
        return
    if update_fields is None or "name" in update_fields:
        with transaction.atomic():
            for class_instance in instance.classes_in_subcategory.iterator():
                new_vector = get_classesmain_search_vector(class_instance)
                if class_instance.search_vector != new_vector:
                    ClassesMain.objects.filter(pk=class_instance.pk).update(
                        search_vector=new_vector
                    )


class Favorites(models.Model):
    favoriteId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="favorites_through_model"
    )
    classId = models.ForeignKey(
        ClassesMain,
        models.CASCADE,
        db_column="classId",
        related_name="favorited_by_records",
    )
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "favorites"
        unique_together = ("userId", "classId")
        indexes = [models.Index(fields=["userId", "-createdAt"])]


class ClassOption(models.Model):
    optionId = models.AutoField(primary_key=True)
    classId = models.ForeignKey(
        "ClassesMain", on_delete=models.CASCADE, related_name="options"
    )

    BOOKING_TYPES = [
        ("Single Session", "Single Session"),
        # ("Full Course", "Full Course"), # Kept for potential future use
    ]
    booking_type = models.CharField(
        max_length=20, choices=BOOKING_TYPES, default="Single Session"
    )

    level = models.CharField(
        max_length=20,
        choices=[
            ("beginner", "Beginner"),
            ("intermediate", "Intermediate"),
            ("advanced", "Advanced"),
            ("all", "All Levels"),
        ],
        default="all",
    )

    PRICE_TYPES = [("per_session", "Per Session"), ("full_course", "Full Course")]
    price_type = models.CharField(
        max_length=20, choices=PRICE_TYPES, default="per_session"
    )

    # Additional Info
    equipment = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)

    CANCELLATION_POLICY_CHOICES = [
        ("flexible", "Flexible (up to 1 hour before)"),
        ("24h", "24 Hours Notice"),
        ("48h", "48 Hours Notice"),
        ("72h", "72 Hours Notice"),
        ("strict", "Strict (Non-refundable)"),
    ]
    cancellationPolicy = models.CharField(
        max_length=30,
        choices=CANCELLATION_POLICY_CHOICES,
        default="flexible",
    )
    cancellationRefundPercentage = models.PositiveIntegerField(
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of refund if cancellation policy conditions are met (0-100).",
    )

    createdAt = models.DateTimeField(auto_now_add=True)
    updatedAt = models.DateTimeField(auto_now=True)

    # Property to access parent class title
    @property
    def parent_class_title(self):
        return self.classId.title

    @property
    def parent_class_description(self):
        return self.classId.description

    def __str__(self):
        # Refer to the class's title for identification
        return f"Option for {self.classId.title} (ID: {self.optionId})"

    class Meta:
        db_table = "class_options"
        indexes = [models.Index(fields=["classId"])]


class Schedule(models.Model):
    name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="An optional name for this schedule, e.g., 'Weekend Morning Session'.",
    )
    option = models.ForeignKey(
        ClassOption, on_delete=models.CASCADE, related_name="schedules"
    )
    day = models.CharField(
        max_length=3,
        choices=[
            ("Mon", "Monday"),
            ("Tue", "Tuesday"),
            ("Wed", "Wednesday"),
            ("Thu", "Thursday"),
            ("Fri", "Friday"),
            ("Sat", "Saturday"),
            ("Sun", "Sunday"),
        ],
        null=True,
        blank=True,
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

    allow_late_enrollment = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        self.clean()

        if self.option.booking_type != "Full Course" and self.date:
            self.day = self.date.strftime("%a")

        super().save(*args, **kwargs)

        # The creation logic that caused the IntegrityError in tests has been removed.
        # The update logic below will only run for existing instances (when is_new is False).

        if not is_new:
            # Instance UPDATE logic
            if self.option.booking_type != "Full Course":
                try:
                    instance = self.instances.get()
                    fields_to_update = {}
                    if instance.date != self.date:
                        fields_to_update["date"] = self.date
                    if instance.time != self.time:
                        fields_to_update["time"] = self.time
                    if instance.duration != self.duration:
                        fields_to_update["duration"] = self.duration
                    if instance.price != self.price:
                        fields_to_update["price"] = self.price
                    if instance.max_participants != self.maxParticipants:
                        fields_to_update["max_participants"] = self.maxParticipants

                    if fields_to_update:
                        if instance.bookings.filter(status="confirmed").exists():
                            logger.warning(
                                f"Updating ScheduleInstance {instance.id} which has confirmed bookings. Bookings NOT automatically modified."
                            )

                        for field, value in fields_to_update.items():
                            setattr(instance, field, value)
                        instance.save(update_fields=fields_to_update.keys())
                        logger.info(
                            f"Updated ScheduleInstance {instance.id} for single session Schedule {self.id}"
                        )

                except ScheduleInstance.DoesNotExist:
                    logger.warning(
                        f"Instance not found for updated single session Schedule {self.id}. Creating one."
                    )
                    if self.date:
                        ScheduleInstance.objects.create(
                            schedule=self,
                            date=self.date,
                            time=self.time,
                            duration=self.duration,
                            price=self.price,
                            max_participants=self.maxParticipants,
                            status="scheduled",
                        )
                except ScheduleInstance.MultipleObjectsReturned:
                    logger.error(
                        f"CRITICAL: Multiple instances found for single session Schedule {self.id}. Manual correction needed."
                    )
            else:  # Full Course Update
                future_instances = self.instances.filter(
                    date__gte=timezone.now().date(), status="scheduled"
                )
                updated_count = 0
                for instance in future_instances:
                    instance_changed = False
                    if instance.time != self.time:
                        instance.time = self.time
                        instance_changed = True
                    if instance.duration != self.duration:
                        instance.duration = self.duration
                        instance_changed = True
                    if instance.price != self.price:
                        instance.price = self.price
                        instance_changed = True
                    if instance.max_participants != self.maxParticipants:
                        instance.max_participants = self.maxParticipants
                        instance_changed = True

                    if instance_changed:
                        instance.save()
                        updated_count += 1
                if updated_count > 0:
                    logger.info(
                        f"Updated {updated_count} future instances for course Schedule {self.pk}"
                    )

    def delete(self, *args, **kwargs):
        schedule_id_log = self.pk
        option_title_log = self.option.classId.title if self.option else "N/A"

        # Log before actual deletion attempt
        logger.info(
            f"Attempting to delete Schedule ID {schedule_id_log} for option of class '{option_title_log}'. Associated instances will also be deleted."
        )

        super().delete(*args, **kwargs)  # This will trigger cascaded deletes.
        logger.info(
            f"Schedule ID {schedule_id_log} for option of class '{option_title_log}' successfully deleted."
        )

    def generate_course_instances(self):
        """Generate all instances for a course between start and end date"""
        if self.option.booking_type != "Full Course":
            return []

        if not (self.start_date and self.end_date and self.day):
            raise ValidationError(
                "Start date, end date, and day required for course schedules"
            )

        day_to_number = {
            "Mon": 0,
            "Tue": 1,
            "Wed": 2,
            "Thu": 3,
            "Fri": 4,
            "Sat": 5,
            "Sun": 6,
        }

        target_weekday = day_to_number[self.day]
        current_date = self.start_date

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
                duration=self.duration,
            )
            instances.append(instance)
            current_date += timedelta(weeks=1)

        created_instances = ScheduleInstance.objects.bulk_create(instances)
        logger.info(
            f"Generated {len(created_instances)} instances for course Schedule ID {self.id}"
        )
        return created_instances

    def clean(self):
        if self.option.booking_type == "Full Course":
            if not all([self.start_date, self.end_date, self.day]):
                raise ValidationError(
                    {
                        "course_dates": "Start date, end date, and day required for courses"
                    }
                )
            if self.start_date and self.end_date and self.start_date >= self.end_date:
                raise ValidationError(
                    {"course_dates": "End date must be after start date"}
                )
        else:  # Single Session
            if not self.date:
                raise ValidationError({"date": "Date is required for single sessions"})
            if self.date and not self.day:
                self.day = self.date.strftime("%a")

    class Meta:
        db_table = "schedules"
        ordering = ["day", "time"]
        indexes = [
            models.Index(fields=["option", "day", "time"]),
            models.Index(fields=["name"]),
        ]


class ScheduleInstance(models.Model):
    """Specific occurrence of a schedule"""

    schedule = models.ForeignKey(
        Schedule, on_delete=models.CASCADE, related_name="instances"
    )
    date = models.DateField()
    time = models.TimeField()
    duration = models.IntegerField(default=60)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    max_participants = models.IntegerField()

    STATUS_CHOICES = [
        ("scheduled", "Scheduled"),
        ("cancelled", "Cancelled"),
        ("completed", "Completed"),
    ]
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="scheduled"
    )
    cancellation_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            # Find bookings associated with this specific instance
            bookings_to_cancel = Booking.objects.filter(
                schedule_instance=self,
                status__in=[
                    "pending",
                    "confirmed",
                ],  # Cancel pending and confirmed bookings
            )

            logger.info(
                f"Processing deletion for ScheduleInstance {self.id}. Found {bookings_to_cancel.count()} bookings to update."
            )

            for booking in bookings_to_cancel:
                original_payment_status = booking.payment_status
                booking.status = "cancelled"
                booking.cancelled_at = timezone.now()
                booking.cancellation_reason = (
                    "Session instance was removed by the instructor."
                )

                update_fields_for_booking = [
                    "status",
                    "cancelled_at",
                    "cancellation_reason",
                ]

                if original_payment_status == "paid":
                    booking.payment_status = "refund_pending"
                    update_fields_for_booking.append("payment_status")
                    logger.info(
                        f"Booking {booking.id} for instance {self.id} marked for refund (payment_status set to refund_pending)."
                    )

                booking.save(update_fields=update_fields_for_booking)
                # TODO: Send notification to user about cancellation and refund status
                # This might involve a signal or a direct call to a notification utility.
                logger.info(f"Booking {booking.id} status updated to 'cancelled'.")

            # Now proceed with deletion of the instance itself
            instance_id_log = self.id
            super().delete(*args, **kwargs)
            logger.info(
                f"ScheduleInstance {instance_id_log} deleted and its bookings processed."
            )

    @property
    def current_bookings(self):
        return self.bookings.filter(status="confirmed").aggregate(
            total_participants=Coalesce(Sum("participants"), 0)
        )["total_participants"]

    @property
    def available_spots(self):
        return self.max_participants - self.current_bookings

    @classmethod
    def get_available_in_range(cls, option_id, start_date, end_date):
        """Get available instances within a date range"""
        return (
            cls.objects.filter(
                schedule__option_id=option_id,
                # schedule__is_active=True, # No longer needed
                date__range=(start_date, end_date),
                status="scheduled",
            )
            .select_related("schedule")
            .order_by("date", "time")
        )

    def can_accommodate(self, requested_participants):
        """Check if instance can accommodate requested number of participants"""
        return self.available_spots >= requested_participants

    class Meta:
        db_table = "schedule_instances"
        unique_together = ["schedule", "date"]
        indexes = [
            models.Index(fields=["schedule", "date"]),
            models.Index(fields=["schedule", "status"]),
            models.Index(fields=["date", "time"]),
            models.Index(fields=["schedule", "date", "status"]),
        ]


class Booking(models.Model):
    id = models.AutoField(primary_key=True)
    user_facing_reference = models.CharField(
        max_length=20, unique=True, editable=False, db_index=True, null=True, blank=True
    )
    booking_group_id = models.UUIDField(null=True, blank=True)
    schedule_instance = models.ForeignKey(
        "ScheduleInstance",
        on_delete=models.CASCADE,
        related_name="bookings",  # Use string if defined later
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings"
    )

    enrollment_type = models.CharField(
        max_length=20,
        choices=[
            ("Single Session", "Single Session"),
            ("Full Course", "Full Course"),
        ],
        default="Single Session",
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("confirmed", "Confirmed"),
            ("cancelled", "Cancelled"),
            ("completed", "Completed"),
        ],
        default="pending",
    )
    payouts = models.ManyToManyField("Payout", related_name="bookings")
    payout_status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("processed", "Processed"),
            ("failed", "Failed"),
            ("not_applicable", "Not Applicable"),  # For free classes
        ],
        default="pending",
        db_index=True,
        help_text="Tracks the payout status for this specific booking.",
    )

    booking_date = models.DateTimeField(auto_now_add=True)
    participants = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(4)]
    )
    participant_details = models.JSONField(
        default=list,
        blank=True,
        help_text="List of dicts for participant names, e.g., [{'name': 'Jane Doe'}, {'name': 'John Smith'}]",
    )
    notes = models.TextField(blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    cancellation_policy = models.CharField(
        max_length=30,
        choices=CANCELLATION_POLICY_CHOICES,  # Assuming this is defined in your models.py
        default="flexible",
        help_text="The cancellation policy snapshotted at the time of booking.",
    )
    cancellation_refund_percentage = models.PositiveIntegerField(
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="The refund percentage snapshotted at the time of booking.",
    )

    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("paid", "Paid"),
            ("refund_pending", "Refund Pending"),
            ("refunded", "Refunded"),
        ],
        default="pending",
    )

    def _generate_user_facing_reference(self):
        # Generate a unique reference, e.g., BKG-XXXXXX
        # Ensure it's unique before saving.
        while True:
            prefix = "BKG-"  # Or your preferred prefix
            random_part = "".join(
                random.choices(string.ascii_uppercase + string.digits, k=6)
            )
            reference = f"{prefix}{random_part}"
            if not Booking.objects.filter(user_facing_reference=reference).exists():
                return reference

    def save(self, *args, **kwargs):
        if not self.user_facing_reference and self.status == "confirmed":
            # Generate reference only when booking is confirmed (e.g., by webhook)
            self.user_facing_reference = self._generate_user_facing_reference()
        super().save(*args, **kwargs)

    class Meta:
        db_table = "bookings"
        indexes = [
            models.Index(fields=["schedule_instance", "status"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["booking_date"]),
            models.Index(fields=["enrollment_type", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["booking_group_id"]),
            models.Index(fields=["payout_status"]),
            models.Index(fields=["user_facing_reference"]),  # Index new field
        ]
        permissions = [
            ("cancel_any_booking", "Can cancel any user's booking (Admin)"),
            ("view_booking_analytics", "Can view aggregated booking analytics"),
            ("export_booking_data", "Can export booking data"),
            ("access_booking_admin", "Can access the Booking Administration section"),
            (
                "view_own_booking_analytics",
                "Can view booking analytics for own business",
            ),
            ("cancel_business_booking", "Can cancel bookings within own business"),
        ]


class Payment(models.Model):
    """Stores detailed payment information"""

    id = models.AutoField(primary_key=True)
    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="payments"
    )

    # Stripe specific fields
    stripe_payment_intent_id = models.CharField(max_length=255, unique=True)
    stripe_charge_id = models.CharField(max_length=255, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    service_fee_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="The portion of the payment amount that is the platform's service fee.",
    )
    currency = models.CharField(max_length=3, default="USD")

    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("succeeded", "Succeeded"),
            ("failed", "Failed"),
            ("refunded", "Refunded"),
            ("partially_refunded", "Partially Refunded"),
        ],
        default="pending",
    )

    # Payment method details
    payment_method_type = models.CharField(max_length=20, default="card")
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
        return self.status.replace("_", " ").title()

    @property
    def is_refundable(self):
        return self.status == "succeeded" and self.refunded_amount < self.amount

    @property
    def available_refund_amount(self):
        return self.amount - self.refunded_amount

    class Meta:
        db_table = "payments"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stripe_payment_intent_id"]),
            models.Index(fields=["stripe_charge_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
        ]
        permissions = [
            # --- Platform Admin Permissions (Keep these) ---
            ("process_refund", "Can process refunds for any payment"),
            ("mark_payment_paid", "Can manually mark a payment as paid"),
            ("view_payment_stats", "Can view aggregated payment statistics"),
            ("export_payment_data", "Can export payment data"),
            ("access_payment_admin", "Can access the Payment Administration section"),
        ]


class Payout(models.Model):
    """
    Records a payout transfer made from the platform to a business's Stripe account.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business = models.ForeignKey(
        BusinessInfo, on_delete=models.PROTECT, related_name="payouts"
    )
    stripe_transfer_id = models.CharField(max_length=255, unique=True, db_index=True)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Amount transferred to the business in the specified currency.",
    )
    currency = models.CharField(max_length=3)
    arrival_date = models.DateField()
    status = models.CharField(
        max_length=30,
        help_text="Status of the transfer from Stripe (e.g., pending, paid, failed).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Payout of {self.amount} {self.currency} to {self.business.businessName} ({self.status})"

    class Meta:
        db_table = "payouts"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["business"]),
            models.Index(fields=["status"]),
            models.Index(fields=["arrival_date"]),
        ]
        permissions = [
            ("access_payout_admin", "Can access the Payout Administration section"),
            ("view_payout_analytics", "Can view aggregated payout analytics"),
            ("export_payout_data", "Can export payout data"),
            ("trigger_manual_payout", "Can trigger a manual payout process"),
            ("retry_failed_payout", "Can retry a failed payout transfer"),
        ]


class Reviews(models.Model):
    reviewId = models.AutoField(primary_key=True)
    userId = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="reviews"
    )
    businessId = models.ForeignKey(
        BusinessInfo,
        models.CASCADE,
        db_column="businessId",
        related_name="reviews_directly_to_business",
    )  # Retaining, ensure population logic
    classId = models.ForeignKey(
        ClassesMain, models.CASCADE, db_column="classId", related_name="reviews"
    )
    booking = models.OneToOneField(
        Booking, on_delete=models.SET_NULL, related_name="review", null=True, blank=True
    )
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField()
    image = models.ImageField(
        upload_to="originals/review_images/",
        null=True,
        blank=True,
        max_length=255,
        help_text="Image uploaded by the user, e.g., a photo from the class.",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("approved", "Approved"),
            ("under_review", "Under Review"),
            ("hidden", "Hidden"),
        ],
        default="approved",  # Or 'under_review' if you want manual approval first
    )
    # --- New/Updated Fields for Business Review Management ---
    business_response = models.TextField(blank=True, null=True)
    reported = models.BooleanField(default=False)
    report_reason = models.TextField(blank=True, null=True)
    responded_at = models.DateTimeField(
        null=True, blank=True
    )  # When business responded
    reported_at = models.DateTimeField(null=True, blank=True)  # When business reported
    # --- End New/Updated Fields ---
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reviews"
        indexes = [
            models.Index(fields=["classId"]),
            models.Index(fields=["booking"]),
            models.Index(fields=["status"]),  # Index on status
            models.Index(fields=["reported"]),  # Index on reported
        ]
        permissions = [
            # --- Platform Admin Permissions ---
            ("access_review_admin", "Can access the Review Moderation section"),
            # --- Business User Permissions ---
            ("view_own_business_reviews", "Can view reviews related to own business"),
            (
                "add_business_review_response",
                "Can add/edit responses to reviews on own classes",
            ),
        ]

    def save(self, *args, **kwargs):
        # If business_response is being added/changed and was previously empty/different
        if (
            self.pk
            and "business_response" in kwargs.get("update_fields", [])
            or (not self.pk and self.business_response)
        ):  # Check for update_fields if provided
            is_new_response = False
            if self.pk:
                try:
                    old_instance = Reviews.objects.get(pk=self.pk)
                    if old_instance.business_response != self.business_response:
                        is_new_response = True
                except Reviews.DoesNotExist:
                    is_new_response = True  # Should not happen if self.pk exists
            else:  # New instance with a response
                is_new_response = True

            if is_new_response:
                self.responded_at = timezone.now()

        # If reported is being set to True and was previously False
        if (
            self.pk
            and "reported" in kwargs.get("update_fields", [])
            or (not self.pk and self.reported)
        ):
            is_new_report = False
            if self.pk:
                try:
                    old_instance = Reviews.objects.get(pk=self.pk)
                    if not old_instance.reported and self.reported:
                        is_new_report = True
                except Reviews.DoesNotExist:
                    is_new_report = True
            else:  # New instance being reported
                is_new_report = True

            if is_new_report:
                self.reported_at = timezone.now()
                self.status = (
                    "under_review"  # Automatically set to under_review when reported
                )

        super().save(*args, **kwargs)


class SupportTicket(models.Model):
    """
    Represents a customer support ticket.
    """

    # Core Fields
    ticket_id = models.AutoField(primary_key=True)
    user_facing_id = models.CharField(
        max_length=15, unique=True, editable=False, db_index=True
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=255)
    description = models.TextField(help_text="Initial description of the issue.")

    # Categorization
    CATEGORY_CHOICES = [
        ("account", "Account Issues"),
        ("booking", "Booking Problems"),
        ("payment", "Payment Issues"),
        ("technical", "Technical Support"),
        ("feature", "Feature Request"),
        ("other", "Other"),
    ]
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)

    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_progress", "In Progress"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    ]
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="open", db_index=True
    )

    PRIORITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]
    priority = models.CharField(
        max_length=20, choices=PRIORITY_CHOICES, default="medium", db_index=True
    )

    # Management & Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    first_agent_response_at = models.DateTimeField(null=True, blank=True)
    last_user_reply_at = models.DateTimeField(null=True, blank=True)
    last_agent_reply_at = models.DateTimeField(null=True, blank=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        limit_choices_to={"role__name__in": ["Admin", "Super Admin", "Support Agent"]},
    )
    resolution_notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.user_facing_id:
            last_ticket = SupportTicket.objects.all().order_by("ticket_id").last()
            new_id = (last_ticket.ticket_id if last_ticket else 0) + 10001
            self.user_facing_id = f"SPT-{new_id}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.user_facing_id}] {self.subject}"

    class Meta:
        db_table = "support_tickets"
        ordering = ["-updated_at"]
        permissions = [
            ("access_support_admin", "Can access Support Ticket Administration"),
            ("assign_support_ticket", "Can assign any support ticket to an agent"),
            ("view_support_ticket_stats", "Can view support ticket statistics"),
            ("export_support_ticket_data", "Can export support ticket data"),
            ("view_assignable_agents", "Can view list of assignable support agents"),
        ]


class TicketMessage(models.Model):
    """
    Represents a single message within a support ticket's conversation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="conversation"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    SENDER_TYPE_CHOICES = [("user", "User"), ("agent", "Agent")]
    sender_type = models.CharField(max_length=10, choices=SENDER_TYPE_CHOICES)
    text = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new:
            ticket = self.ticket
            ticket.updated_at = self.timestamp
            update_fields = ["updated_at"]
            if self.sender_type == "user":
                ticket.last_user_reply_at = self.timestamp
                update_fields.append("last_user_reply_at")
            elif self.sender_type == "agent":
                ticket.last_agent_reply_at = self.timestamp
                update_fields.append("last_agent_reply_at")
                if not ticket.first_agent_response_at:
                    ticket.first_agent_response_at = self.timestamp
                    update_fields.append("first_agent_response_at")
            ticket.save(update_fields=update_fields)

    class Meta:
        db_table = "support_ticket_messages"
        ordering = ["timestamp"]


class TicketHistoryLog(models.Model):
    """
    An audit trail for actions performed on a support ticket.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="history_logs"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="User who performed the action (can be system)",
    )
    user_email = models.CharField(
        max_length=255, help_text="Snapshot of the user's email"
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.CharField(max_length=512, help_text="Description of the event")

    def __str__(self):
        return f"Log for Ticket {self.ticket.user_facing_id} at {self.timestamp}"

    class Meta:
        db_table = "support_ticket_history"
        ordering = ["-timestamp"]


class BlogCategory(models.Model):
    """Model for blog post categories."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(
        max_length=120, unique=True, help_text="URL-friendly identifier."
    )

    def __str__(self):
        return self.name

    class Meta:
        db_table = "blog_categories"
        verbose_name_plural = "Blog Categories"
        ordering = ["name"]
        permissions = [
            ("manage_blog_categories", "Can create, edit, and delete blog categories"),
        ]


class BlogPost(models.Model):
    """Model for a single blog post."""

    slug = models.SlugField(max_length=255, unique=True, db_index=True)
    title = models.CharField(max_length=200)
    excerpt = models.TextField(max_length=500, help_text="A short summary of the post.")
    content = models.TextField(
        help_text="The full content of the blog post in HTML format."
    )
    image_url = models.URLField(
        max_length=1024, help_text="URL for the main post image."
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="blog_posts",
    )
    category = models.ForeignKey(
        BlogCategory, on_delete=models.PROTECT, related_name="posts"
    )
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="A list of tags, e.g., ['Community', 'Education']",
    )

    STATUS_CHOICES = [("published", "Published"), ("draft", "Draft")]
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default="draft", db_index=True
    )

    published_date = models.DateTimeField(
        default=timezone.now, help_text="The date and time the post is published."
    )
    read_time = models.PositiveIntegerField(
        default=0, help_text="Estimated read time in minutes."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Estimate read time
        word_count = len(self.content.split())
        self.read_time = math.ceil(word_count / 230)  # Average reading speed

        # Auto-generate slug if it's not set
        if not self.slug:
            self.slug = django_slugify(self.title)
            # Ensure slug is unique
            original_slug = self.slug
            counter = 1
            while BlogPost.objects.filter(slug=self.slug).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1

        super().save(*args, **kwargs)

    class Meta:
        ordering = ["-published_date"]
        db_table = "blog_posts"
        permissions = [
            ("manage_blog_posts", "Can create, edit, and delete blog posts"),
        ]


class NotificationCampaign(models.Model):
    """Notification campaign records"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    celery_task_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="ID of the master Celery task for sending this campaign.",
    )
    title = models.CharField(max_length=255)

    NOTIFICATION_TYPES = [
        ("email", "Email"),
        ("push", "Push Notification"),
        ("in_app", "In-App Notification"),
        ("sms", "SMS"),
    ]
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)

    # Content
    subject = models.CharField(max_length=255)
    content = models.TextField()

    # For email content
    html_content = models.TextField(blank=True, null=True)

    # Audience targeting
    AUDIENCE_TYPES = [
        ("all_users", "All Users"),
        ("segment", "User Segment"),
        ("individual", "Individual Users"),
    ]
    audience_type = models.CharField(max_length=20, choices=AUDIENCE_TYPES)
    segment = models.CharField(max_length=100, blank=True, null=True)

    # For individual users targeting - store as JSON array of user IDs
    target_user_ids = models.JSONField(default=list, blank=True, null=True)

    # Status
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")

    # Scheduling
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # Metadata
    recipient_count = models.IntegerField(default=0)
    delivered_count = models.IntegerField(default=0)
    click_count = models.IntegerField(default=0, help_text="Count of unique clicks.")
    success_rate = models.FloatField(default=0.0)

    created_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_notifications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # For error tracking
    error_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"

    class Meta:
        db_table = "notification_campaigns"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["notification_type"]),
            models.Index(fields=["scheduled_for"]),
            models.Index(fields=["sent_at"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["celery_task_id"]),
        ]
        permissions = [
            # --- Platform Admin Permissions ---
            ("send_notification_campaign", "Can send notification campaigns"),
            ("cancel_notification_campaign", "Can cancel scheduled campaigns"),
            ("duplicate_notification_campaign", "Can duplicate campaigns"),
            ("view_notification_metrics", "Can view notification campaign metrics"),
            ("access_notification_admin", "Can access Notification Management section"),
        ]


class NotificationAttachment(models.Model):
    """Attachments for email notifications"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey(
        NotificationCampaign, on_delete=models.CASCADE, related_name="attachments"
    )
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to="notification_attachments/")
    content_type = models.CharField(max_length=100)
    size = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        db_table = "notification_attachments"


class UserSegment(models.Model):
    """User segments for targeting notifications"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    # Define the criteria for this segment as a JSON object
    criteria = models.JSONField(default=dict)

    # Cache the number of users in this segment
    user_count = models.IntegerField(default=0)

    created_by = models.ForeignKey(
        "CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_segments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.user_count} users)"

    class Meta:
        db_table = "user_segments"
        ordering = ["name"]
        permissions = [
            # --- Platform Admin Permissions ---
            ("view_segment_users", "Can view users within a segment"),
            ("refresh_segment_counts", "Can trigger recalculation of segment counts"),
            ("access_segment_admin", "Can access User Segment Management section"),
        ]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    business = models.ForeignKey(
        "BusinessInfo",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="business_notifications",
        help_text="Business context for this notification, if applicable.",
    )

    NOTIFICATION_TYPE_CHOICES = [
        ("new_booking", "New Booking"),
        ("booking_cancelled_by_user", "Booking Cancelled by User"),
        ("booking_cancelled_by_biz", "Booking Cancelled by Business"),
        ("class_reminder_biz", "Class Reminder for Business"),  # For business owner
        ("class_reminder_student", "Class Reminder for Student"),  # For student
        ("new_review", "New Review"),
        ("review_response", "Review Response from Business"),  # For student
        ("payment_succeeded", "Payment Succeeded"),
        ("payment_failed", "Payment Failed"),
        ("payout_initiated", "Payout Initiated"),  # Example for future
        ("stripe_action_required", "Stripe Action Required"),
        ("profile_incomplete", "Profile Incomplete"),
        ("system_announcement", "System Announcement"),
        ("new_message_support", "New Message in Support Ticket"),
        # Add more types as needed
    ]
    notification_type = models.CharField(
        max_length=50, choices=NOTIFICATION_TYPE_CHOICES
    )
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.CharField(
        max_length=255, null=True, blank=True
    )  # Use CharField for UUIDs or int IDs
    source_object = GenericForeignKey("content_type", "object_id")

    # For frontend display
    icon = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="e.g., Lucide icon name like 'UserPlus'",
    )  # From your BusinessDashboardOverviewSerializer
    color = models.CharField(
        max_length=20, blank=True, null=True, help_text="e.g., '#3b82f6'"
    )
    link_web = models.CharField(
        max_length=255, blank=True, null=True, help_text="Relative URL for web client"
    )

    def __str__(self):
        return f"Notification for {self.user.email} - Type: {self.get_notification_type_display()}"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "is_read", "-created_at"]),
            models.Index(fields=["business", "is_read", "-created_at"]),
            models.Index(fields=["notification_type"]),
        ]
        db_table = "notifications"
