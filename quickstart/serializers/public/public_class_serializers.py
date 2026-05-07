import os
from django.conf import settings
from rest_framework import serializers

from quickstart.utils.url_utils import build_cloudfront_url
from quickstart.models import (
    ClassCollection,
    ClassesMain,
    ClassImage,
    ClassOption,
    Schedule,
    ClassCategory,
    ImportedGoogleReview,
    Favorites,
)

# Important: Import the Google review serializer
from .public_review_serializers import (
    PublicReviewSerializer,
    ImportedGoogleReviewSerializer,
)
from django.utils import timezone
import logging
from random import uniform
from decimal import Decimal
from django.db.models import Count, Avg

logger = logging.getLogger(__name__)

# Warn about missing CLOUDFRONT_DOMAIN only once per process to avoid log spam (e.g. during cache prewarm).
_cloudfront_warned = False


def _warn_cloudfront_once():
    global _cloudfront_warned
    if not _cloudfront_warned and not getattr(settings, "CLOUDFRONT_DOMAIN", None):
        logger.warning("CLOUDFRONT_DOMAIN is not configured in settings.py")
        _cloudfront_warned = True


class PublicClassImageKeySerializer(serializers.ModelSerializer):
    """
    Lightweight serializer for list/card views: imageId, image_key, medium_url (CloudFront), isCover.
    Class list/search/homepage responses include stable CloudFront URLs so the frontend
    can use them directly; no separate image-url endpoint needed. CloudFront caches at the edge.
    isCover lets the frontend show the correct cover image first (e.g. on business page upcoming classes).
    """
    image_key = serializers.SerializerMethodField()
    medium_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = ["imageId", "image_key", "medium_url", "isCover"]
        read_only_fields = fields

    def get_image_key(self, obj):
        if obj.image and obj.image.name:
            return obj.image.name
        return None

    def _get_resized_url(self, obj, size_name):
        """CloudFront URL for resized WebP (same convention as PublicClassImageSerializer)."""
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            return None
        if obj.image and obj.image.name:
            original_path = obj.image.name
            if not original_path.startswith("originals/"):
                return None
            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return build_cloudfront_url(final_path)
        return None

    def get_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")


class PublicClassImageSerializer(serializers.ModelSerializer):
    """
    Serializer for publicly displaying class images with URLs (detail view only).
    Not used in cached list/search responses — those use PublicClassImageKeySerializer.
    """

    original_url = serializers.ImageField(source="image", read_only=True)
    thumbnail_url = serializers.SerializerMethodField()
    medium_url = serializers.SerializerMethodField()
    large_url = serializers.SerializerMethodField()

    class Meta:
        model = ClassImage
        fields = [
            "imageId",
            "original_url",
            "thumbnail_url",
            "medium_url",
            "large_url",
        ]
        read_only_fields = fields

    def _get_resized_url(self, obj, size_name):
        """
        Constructs a public CloudFront URL for a resized WebP image.
        """
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            _warn_cloudfront_once()
            return None

        if obj.image and obj.image.name:
            original_path = obj.image.name

            if not original_path.startswith("originals/"):
                return None

            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return build_cloudfront_url(final_path)

        return None

    def get_thumbnail_url(self, obj):
        return self._get_resized_url(obj, "thumb")

    def get_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")

    def get_large_url(self, obj):
        return self._get_resized_url(obj, "large")


class PublicScheduleSerializer(serializers.ModelSerializer):
    """Serializer for publicly displaying basic schedule info."""

    available_spots = serializers.SerializerMethodField()
    instance_id = serializers.SerializerMethodField()

    def _matching_schedule_instance(self, obj):
        """
        Schedule row `obj.date` maps to a ScheduleInstance row for booking/payment APIs.
        Must stay in sync with get_available_spots / get_instance_id.
        """
        try:
            prefetched = getattr(obj, "_prefetched_instances", None)
            if prefetched is not None:
                instances = [
                    i
                    for i in prefetched
                    if str(i.date) == str(obj.date) and i.status == "scheduled"
                ]
            else:
                instances = list(
                    obj.instances.filter(date=obj.date, status="scheduled")
                )
            if instances:
                return instances[0]
        except Exception:
            pass
        return None

    def get_available_spots(self, obj):
        """
        Return spots still bookable for this schedule's session date.
        Uses the prefetched + annotated ScheduleInstance list (_prefetched_instances)
        to avoid N+1 DB queries. Falls back to maxParticipants when no instance
        exists (e.g. recurring patterns without a concrete instance yet).
        """
        inst = self._matching_schedule_instance(obj)
        if inst:
            if hasattr(inst, "total_booked"):
                return max(0, inst.max_participants - inst.total_booked)
            return max(0, inst.available_spots)
        return obj.maxParticipants

    def get_instance_id(self, obj):
        """ScheduleInstance PK — required for booking (distinct from Schedule.id)."""
        inst = self._matching_schedule_instance(obj)
        return inst.id if inst else None

    class Meta:
        model = Schedule
        fields = [
            "id",
            "instance_id",
            "day",
            "time",
            "duration",
            "price",
            "minParticipants",
            "maxParticipants",
            "available_spots",
            "start_date",
            "end_date",
            "date",
            "allow_late_enrollment",
        ]
        read_only_fields = fields


class PublicClassOptionSerializer(serializers.ModelSerializer):
    """
    Serializer for publicly displaying class options.
    This serializer is now lean and does NOT include schedules, for use in LIST views.
    """

    def to_representation(self, instance):
        """Ensure equipment is always returned as string (packing list)."""
        ret = super().to_representation(instance)
        equipment = ret.get("equipment")
        if isinstance(equipment, list):
            ret["equipment"] = "\n".join(str(item) for item in equipment).strip()
        elif equipment is None:
            ret["equipment"] = ""
        return ret

    class Meta:
        model = ClassOption
        fields = [
            "optionId",
            "title",       
            "description",  
            "booking_type",
            "level",
            "equipment",
            "tags",
            "cancellationPolicy",
            "cancellationCustomHours",
            "cancellationRefundPercentage",
            "price_type",
        ]
        read_only_fields = fields


class PublicClassOptionWithSchedulesSerializer(PublicClassOptionSerializer):
    """
    Extends the basic option serializer to include its schedules.
    Used ONLY for the class detail view.
    """

    schedules = PublicScheduleSerializer(many=True, read_only=True)

    class Meta(PublicClassOptionSerializer.Meta):
        fields = PublicClassOptionSerializer.Meta.fields + ["schedules"]


class PublicClassSerializer(serializers.ModelSerializer):
    """
    Serializer for the PUBLIC LIST VIEW of classes. Lean and performant.
    Images are returned as imageId + image_key only (no URLs) so cached responses
    never contain expired pre-signed URLs; frontend fetches image URLs separately.
    """

    options = PublicClassOptionSerializer(many=True, read_only=True)
    images = PublicClassImageKeySerializer(many=True, read_only=True)
    average_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    business_timezone = serializers.CharField(
        source="businessId.business_timezone", read_only=True
    )
    business_name = serializers.CharField(
        source="businessId.businessName", read_only=True, allow_null=True
    )
    business_slug = serializers.CharField(
        source="businessId.slug", read_only=True, allow_null=True
    )
    min_session_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    min_course_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True
    )
    listing_duration_minutes = serializers.SerializerMethodField(
        read_only=True,
        help_text="Shortest schedule duration (minutes); set by list queryset annotation when present.",
    )
    coordinates = serializers.SerializerMethodField(read_only=True)
    location = serializers.SerializerMethodField(read_only=True)
    is_favorited = serializers.SerializerMethodField()
    soonest_next_week = serializers.SerializerMethodField()
    student_contact_email = serializers.SerializerMethodField(read_only=True)
    student_contact_phone = serializers.SerializerMethodField(read_only=True)
    require_participant_names = serializers.BooleanField(
        source="businessId.require_participant_names",
        read_only=True,
        default=False,
    )
    location_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ClassesMain
        fields = [
            "classId",
            "slug",
            "business_slug",
            "businessId",
            "business_name",
            "title",
            "description",
            "features",
            "location",
            "location_name",
            "unit_number",
            "coordinates",
            "saltLocation",
            "createdAt",
            "options",
            "images",
            "average_rating",
            "review_count",
            "is_favorited",
            "business_timezone",
            "city",
            "state",
            "min_session_price",
            "min_course_price",
            "soonest_next_week",
            "listing_duration_minutes",
            "student_contact_email",
            "student_contact_phone",
            "require_participant_names",
        ]
        read_only_fields = fields

    def get_location_name(self, obj):
        ref = getattr(obj, "location_ref", None)
        return ref.name if ref else None

    def get_listing_duration_minutes(self, obj):
        v = getattr(obj, "listing_duration_minutes", None)
        return v if v is not None else None

    def _get_business_contact_if_public(self, obj, attr):
        """Expose business contact when contact_privacy is public or public_with_chat (always visible)."""
        business = getattr(obj, "businessId", None)
        privacy = getattr(business, "contact_privacy", None)
        if not business or privacy not in ("public", "public_with_chat"):
            return None
        return getattr(business, attr, None) or None

    def get_student_contact_email(self, obj):
        return self._get_business_contact_if_public(obj, "studentContactEmail")

    def get_student_contact_phone(self, obj):
        return self._get_business_contact_if_public(obj, "studentContactPhone")

    def get_coordinates(self, obj):
        if obj.point is None:
            return None
        try:
            lat, lng = obj.point.y, obj.point.x
            if obj.saltLocation:
                lat += uniform(-0.0005, 0.0005)
                lng += uniform(-0.0005, 0.0005)
            return f"{lat:.8f},{lng:.8f}"
        except (ValueError, TypeError):
            return None

    def get_location(self, obj):
        """
        When saltLocation is True, expose only city/state for privacy; otherwise full address.
        """
        if obj.saltLocation:
            parts = [p for p in [obj.city, obj.state] if p]
            return ", ".join(parts) if parts else None
        return obj.location

    def get_is_favorited(self, obj):
        ids = self.context.get("favorited_ids")
        if ids is not None:
            return obj.pk in ids
        request = self.context.get("request")
        if request and hasattr(request, "user") and request.user.is_authenticated:
            return Favorites.objects.filter(
                userId=request.user, classId=obj
            ).exists()
        return False

    def _get_google_review_stats(self, obj):
        """
        Get Google review stats for combined counts.
        Uses annotated g_count_raw / g_rating_raw when present (e.g. from class search queryset)
        to avoid N+1 queries; otherwise falls back to per-business aggregate.
        """
        # Use annotated fields from get_queryset() when available (class search, homepage, etc.)
        g_count = getattr(obj, "g_count_raw", None)
        g_rating = getattr(obj, "g_rating_raw", None)
        if g_count is not None and g_rating is not None:
            return {"google_count": int(g_count), "google_avg_rating": float(g_rating)}

        if not hasattr(self, "_google_review_stats_cache"):
            self._google_review_stats_cache = {}

        business_id = obj.businessId_id
        if business_id not in self._google_review_stats_cache:
            stats = obj.businessId.imported_google_reviews.aggregate(
                google_count=Count("id"), google_avg_rating=Avg("rating")
            )
            self._google_review_stats_cache[business_id] = stats

        return self._google_review_stats_cache[business_id]

    def get_review_count(self, obj):
        """Return combined review count (platform + Google) with a deterministic offset."""
        platform_count = getattr(obj, "review_count", 0) or 0
        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        original_count = platform_count + google_count

        if original_count == 0:
            return 0

        # Generate a deterministic offset between 10-20 based on classId
        id_str = str(obj.classId)
        py_hash = hash(id_str)
        offset = 10 + (abs(py_hash) % 11)  # abs() handles potential negative hash value

        return original_count + offset

    def get_average_rating(self, obj):
        """Return combined average rating (platform + Google)"""
        platform_avg = getattr(obj, "average_rating", None)
        platform_count = getattr(obj, "review_count", 0) or 0

        google_stats = self._get_google_review_stats(obj)
        google_count = google_stats.get("google_count") or 0
        google_avg_rating = google_stats.get("google_avg_rating") or 0.0

        platform_avg_decimal = (
            Decimal(str(platform_avg)) if platform_avg is not None else Decimal("0.0")
        )
        google_avg_decimal = (
            Decimal(str(google_avg_rating))
            if google_avg_rating is not None
            else Decimal("0.0")
        )

        total_reviews = platform_count + google_count
        if total_reviews == 0:
            return Decimal("0.0")

        total_rating_sum = (platform_avg_decimal * platform_count) + (
            google_avg_decimal * google_count
        )
        combined_avg = total_rating_sum / total_reviews

        return round(combined_avg, 1)

    def get_soonest_next_week(self, obj):
        """
        Optional label for soonest upcoming date/time (e.g. "Mon 6:00 PM").
        Only set when context includes soonest_per_class and this class is in it.
        Used on business detail to show "Upcoming" class cards.
        """
        soonest = self.context.get("soonest_per_class") or {}
        entry = soonest.get(obj.pk)
        if not entry:
            return None
        date_val = entry.get("date")
        time_val = entry.get("time")
        if not date_val or time_val is None:
            return None
        day_str = date_val.strftime("%a") if hasattr(date_val, "strftime") else str(date_val)[:3]
        time_str = time_val.strftime("%I:%M %p").lstrip("0") if hasattr(time_val, "strftime") else str(time_val)
        return f"{day_str} {time_str}"

# Homepage uses same image shape as list: imageId + image_key only (no URLs).
# HomepageClassSerializer.get_images() returns PublicClassImageKeySerializer data.

class HomepageClassSerializer(PublicClassSerializer):
    """
    Highly optimized serializer for homepage cards.
    1. Returns 'images' as a LIST to maintain structure.
    2. The list contains ONLY one object with ONLY the medium_url.
    3. Strips out heavier fields not needed for the card view.
    4. Formats 'location' to be 'City, State' instead of full address.
    5. Optional 'soonest_next_week' when context includes soonest_per_class (for "Happening Next Week").
    """
    images = serializers.SerializerMethodField()
    # OVERRIDE: Use a method field for location to force "City, State" format
    location = serializers.SerializerMethodField()
    soonest_next_week = serializers.SerializerMethodField()

    class Meta(PublicClassSerializer.Meta):
        fields = [
            "classId",
            "slug",
            "business_name",
            "title",
            "location",
            "city",
            "state",
            "coordinates",
            "average_rating",
            "review_count",
            "min_session_price",
            "min_course_price",
            "images",  # Single cover image for card
            "is_favorited",
            "soonest_next_week",
        ]

    def get_images(self, obj):
        """
        Returns a list containing exactly one image object: imageId, image_key, medium_url (CloudFront), isCover.
        Used on business detail for upcoming class cards — always pick the cover image.
        Sort explicitly by isCover so the choice is correct regardless of prefetch/query order.
        """
        all_images = getattr(obj, "images", None)
        if not all_images:
            return []
        image_list = list(all_images.all()) if hasattr(all_images, "all") else list(all_images)
        if not image_list:
            return []
        # Ensure cover is always first: sort by isCover desc, then createdAt (stable order)
        image_list = sorted(
            image_list,
            key=lambda img: (not getattr(img, "isCover", False), getattr(img, "createdAt", None) or ""),
        )
        target_image = image_list[0]
        return [PublicClassImageKeySerializer(target_image).data]

    def get_location(self, obj):
        """
        Returns 'City, State' (e.g., 'Toronto, ON') or just 'City' if state missing.
        Overrides the default behavior of returning the full address.
        """
        parts = [p for p in [obj.city, obj.state] if p]
        if parts:
            return ", ".join(parts)
        return None

    def get_soonest_next_week(self, obj):
        """
        For "Happening Next Week" section: short label for soonest date/time (e.g. "Mon 6:00 PM").
        Only set when context includes soonest_per_class and this class is in it.
        """
        soonest = self.context.get("soonest_per_class") or {}
        entry = soonest.get(obj.pk)
        if not entry:
            return None
        date_val = entry.get("date")
        time_val = entry.get("time")
        if not date_val or time_val is None:
            return None
        day_str = date_val.strftime("%a") if hasattr(date_val, "strftime") else str(date_val)[:3]
        time_str = time_val.strftime("%I:%M %p").lstrip("0") if hasattr(time_val, "strftime") else str(time_val)
        return f"{day_str} {time_str}"


class PublicCollectionMinimalSerializer(serializers.ModelSerializer):
    """Minimal collection info for class detail (name + slug for display and explore link)."""

    class Meta:
        model = ClassCollection
        fields = ["name", "slug"]


class PublicClassDetailSerializer(PublicClassSerializer):
    """
    The serializer for the class DETAIL VIEW (`/api/classes/<id>/`).
    It inherits everything from the list serializer and overrides the `options`
    field to use the new serializer that INCLUDES schedules.

    Reviews are now paginated separately via the reviews endpoint.
    """

    options = PublicClassOptionWithSchedulesSerializer(many=True, read_only=True)
    platform_review_count = serializers.IntegerField(
        source="review_count", read_only=True
    )
    google_review_count = serializers.SerializerMethodField()
    collections = PublicCollectionMinimalSerializer(many=True, read_only=True)

    class Meta(PublicClassSerializer.Meta):
        fields = PublicClassSerializer.Meta.fields + [
            "platform_review_count",
            "google_review_count",
            "collections",
            "description_summary",
            "description_sections",
            "description_ai_status",
        ]

    def get_google_review_count(self, obj):
        google_stats = self._get_google_review_stats(obj)
        return google_stats.get("google_count") or 0


class PublicCollectionSerializer(serializers.ModelSerializer):
    image_medium_url = serializers.SerializerMethodField()
    key = serializers.CharField(source='slug', read_only=True)
    parent_id = serializers.IntegerField(read_only=True, allow_null=True)
    has_children = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()
    active_class_count = serializers.SerializerMethodField()

    class Meta:
        model = ClassCollection
        fields = [
            "id",
            "name",
            "slug",
            "key",
            "parent_id",
            "has_children",
            "children",
            "description",
            "image_medium_url",
            "sort_order",
            "search_aliases",
            "is_searchable",
            "show_in_i_want",
            "show_in_featured_categories",
            "show_on_homepage_rows",
            "icon_name",
            "color",
            "active_class_count",
        ]

    def get_active_class_count(self, obj):
        ann = getattr(obj, "active_class_count", None)
        if ann is not None:
            return int(ann)
        return obj.classes.filter(status="active").distinct().count()

    def get_has_children(self, obj):
        pref = getattr(obj, "_prefetched_objects_cache", None)
        if pref and "children" in pref:
            return len(pref["children"]) > 0
        return obj.children.filter(is_active=True).exists()

    def get_children(self, obj):
        pref = getattr(obj, "_prefetched_objects_cache", None)
        if pref and "children" in pref:
            qs = pref["children"]
        else:
            qs = list(
                obj.children.filter(is_active=True).order_by("sort_order", "name")[:50]
            )
        out = []
        for c in qs:
            if getattr(c, "is_active", True):
                out.append(
                    {
                        "slug": c.slug,
                        "name": c.name,
                        "icon_name": c.icon_name or "",
                    }
                )
        return out 

    def _get_resized_url(self, obj, size_name):
        """
        Constructs a public CloudFront URL for a resized WebP image.
        """
        if not getattr(settings, "CLOUDFRONT_DOMAIN", None):
            _warn_cloudfront_once()
            return None

        if obj.image and obj.image.name:
            original_path = obj.image.name

            if not original_path.startswith("originals/"):
                return None

            base_path, _ = os.path.splitext(original_path)
            resized_base_path = base_path.replace(
                "originals/", f"public/{size_name}/", 1
            )
            final_path = resized_base_path + ".webp"
            return build_cloudfront_url(final_path)

        return None
    
    def get_image_medium_url(self, obj):
        return self._get_resized_url(obj, "medium")