# In a new file or an existing serializers file (e.g., quickstart/serializers/user_serializers.py)
from rest_framework import serializers
from quickstart.models import Notification  # Adjust import path


class NotificationSerializer(serializers.ModelSerializer):
    # You can add human-readable fields or source object details if needed
    notification_type_display = serializers.CharField(
        source="get_notification_type_display", read_only=True
    )
    time_since = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "user",  # Maybe just user_id for brevity if not nesting user details
            "business",  # business_id
            "message",
            "notification_type",
            "notification_type_display",
            "is_read",
            "created_at",
            "time_since",  # For display like "2 minutes ago"
            "icon",
            "color",
            "link_web",
            # 'content_type', # Usually not exposed directly
            # 'object_id',    # Usually not exposed directly
            # 'source_object_details' # If you add a SerializerMethodField for this
        ]
        read_only_fields = [
            "id",
            "user",
            "business",
            "created_at",
            "time_since",
            "notification_type_display",
        ]

    def get_time_since(self, obj):
        from django.utils.timesince import timesince

        return (
            f"{timesince(obj.created_at).split(',')[0]} ago"  # Simplified "2 hours ago"
        )
