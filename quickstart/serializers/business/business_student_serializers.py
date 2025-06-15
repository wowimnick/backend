from decimal import Decimal
from rest_framework import serializers
from ...models import Booking, BusinessInfo, CustomUser, StudentNote, ScheduleInstance


class BusinessStudentNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    author_avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = StudentNote
        fields = [
            "id",
            "content",
            "created_at",
            "author",
            "author_name",
            "author_avatar_url",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "author",
            "author_name",
            "author_avatar_url",
        ]

    def get_author_name(self, obj):
        if obj.author:
            parts = [
                name for name in [obj.author.first_name, obj.author.last_name] if name
            ]
            return " ".join(parts) if parts else obj.author.email
        return "Unknown Author"

    def get_author_avatar_url(self, obj):
        if obj.author and obj.author.avatar and hasattr(obj.author.avatar, "url"):
            try:
                return obj.author.avatar.url
            except ValueError:
                return None
        return None


class BookingHistorySerializer(serializers.ModelSerializer):
    class_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.title", read_only=True
    )
    option_name = serializers.CharField(
        source="schedule_instance.schedule.option.classId.title", read_only=True
    )
    date = serializers.DateField(source="schedule_instance.date", read_only=True)
    time = serializers.TimeField(source="schedule_instance.time", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "class_name",
            "option_name",
            "date",
            "time",
            "status",
        ]


    def get_attendance_status_display(self, obj):
        if obj.status == "cancelled":
            return "Cancelled"
        if not obj.attendance_marked:
            return "Not Marked"
        return "Present" if obj.attended else "Absent"


class BusinessStudentProfileSerializer(serializers.ModelSerializer):
    active_classes = serializers.IntegerField(
        source="active_bookings_count", read_only=True, default=0
    )
    total_classes_taken = serializers.IntegerField(
        source="completed_bookings_count", read_only=True, default=0
    )
    is_active = serializers.BooleanField(
        source="is_active_student", read_only=True
    )
    avatar_url = serializers.SerializerMethodField()

    last_booking_date_this_business = serializers.DateField(
        read_only=True, allow_null=True
    )
    total_spent_this_business = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True, default=Decimal("0.00")
    )

    notes = BusinessStudentNoteSerializer(
        source="notes_for_this_business", many=True, read_only=True
    )

    # Updated to use the new serializer
    booking_history = BookingHistorySerializer(
        many=True, read_only=True, required=False
    )

    class Meta:
        model = CustomUser
        fields = [
            "userId",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "avatar_url",
            "active_classes",
            "total_classes_taken",
            "notes",
            "is_active",
            "createdAt",
            "last_booking_date_this_business",
            "total_spent_this_business",
            "booking_history", # Updated
        ]
        read_only_fields = fields

    def get_avatar_url(self, obj):
        if obj.avatar and hasattr(obj.avatar, "url"):
            try:
                return obj.avatar.url
            except ValueError:
                return None
        return None
