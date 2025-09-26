# quickstart/serializers/business/widget_config_serializer.py

from rest_framework import serializers
from quickstart.models import BusinessInfo

# Define default domains that should always be allowed but hidden from the user UI.
DEFAULT_WIDGET_DOMAINS = {"classeasily.com", "staging.classeasily.com"}


class BusinessWidgetConfigSerializer(serializers.ModelSerializer):
    """
    Serializer for updating the widget_config JSON field and the
    allowed_widget_origins field on the BusinessInfo model.
    """

    # This represents the `allowed_widget_origins` field, which we expect as a string from the frontend
    allowed_widget_origins = serializers.CharField(
        style={"base_template": "textarea.html"}, allow_blank=True, required=False
    )

    class Meta:
        model = BusinessInfo
        # We don't explicitly list widget_config because we'll handle it dynamically
        fields = ["allowed_widget_origins"]

    def update(self, instance, validated_data):
        # Handle allowed_widget_origins separately
        if "allowed_widget_origins" in validated_data:
            origins_string = validated_data.pop("allowed_widget_origins")
            # Get user-defined domains from the input string
            user_origins = {
                origin.strip()
                for origin in origins_string.split("\n")
                if origin.strip()
            }
            # Combine user domains with our default domains, ensuring no duplicates.
            all_origins = sorted(list(user_origins.union(DEFAULT_WIDGET_DOMAINS)))
            instance.allowed_widget_origins = all_origins

        # All other fields passed in validated_data are part of the widget_config
        # We merge them with the existing config to perform a partial update
        current_config = instance.widget_config or {}
        current_config.update(validated_data)
        instance.widget_config = current_config

        instance.save()
        return instance

    def to_internal_value(self, data):
        # We accept any valid key-value pairs for the config
        # so we pass them all through for the update method to handle.
        return data
