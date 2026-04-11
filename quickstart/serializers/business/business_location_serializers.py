import logging

from django.contrib.gis.geos import Point
from django.db import transaction
from rest_framework import serializers

from quickstart.models import BusinessLocation, ClassesMain
from quickstart.utils.business_location_utils import sync_business_profile_from_primary_location

logger = logging.getLogger(__name__)


class BusinessLocationSerializer(serializers.ModelSerializer):
    """CRUD for a single business venue."""

    assigned_classes_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = BusinessLocation
        fields = [
            "id",
            "name",
            "address",
            "unit",
            "city",
            "state",
            "zip_code",
            "latitude",
            "longitude",
            "show_exact_location",
            "is_primary",
            "is_active",
            "assigned_classes_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "assigned_classes_count"]

    def get_assigned_classes_count(self, obj):
        return ClassesMain.objects.filter(location_ref=obj).count()

    def validate(self, data):
        lat = data.get("latitude", getattr(self.instance, "latitude", None))
        lng = data.get("longitude", getattr(self.instance, "longitude", None))
        if (lat is not None and lng is None) or (lng is not None and lat is None):
            raise serializers.ValidationError(
                {
                    "coordinates": "Provide both latitude and longitude, or neither.",
                }
            )
        return data

    def _set_point(self, instance, latitude, longitude):
        if latitude is not None and longitude is not None:
            instance.point = Point(float(longitude), float(latitude), srid=4326)
        else:
            instance.point = None

    @transaction.atomic
    def create(self, validated_data):
        business = validated_data.pop("business", None) or self.context.get("business")
        if business is None:
            raise serializers.ValidationError("Missing business for location create.")

        is_primary = validated_data.get("is_primary", False)
        if is_primary:
            BusinessLocation.objects.filter(business=business, is_primary=True).update(
                is_primary=False
            )
        elif not BusinessLocation.objects.filter(
            business=business, is_primary=True, is_active=True
        ).exists():
            validated_data["is_primary"] = True
            BusinessLocation.objects.filter(business=business, is_primary=True).update(
                is_primary=False
            )

        loc = BusinessLocation(business=business, **validated_data)
        self._set_point(loc, loc.latitude, loc.longitude)
        loc.save()
        if loc.is_primary:
            sync_business_profile_from_primary_location(loc)
        return loc

    @transaction.atomic
    def update(self, instance, validated_data):
        is_primary = validated_data.get("is_primary", instance.is_primary)
        if is_primary and not instance.is_primary:
            BusinessLocation.objects.filter(
                business=instance.business, is_primary=True
            ).exclude(pk=instance.pk).update(is_primary=False)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        lat = instance.latitude
        lng = instance.longitude
        self._set_point(instance, lat, lng)
        instance.save()

        if instance.is_primary:
            sync_business_profile_from_primary_location(instance)

        if not BusinessLocation.objects.filter(
            business=instance.business, is_primary=True, is_active=True
        ).exists():
            nxt = (
                BusinessLocation.objects.filter(
                    business=instance.business, is_active=True
                )
                .order_by("name")
                .first()
            )
            if nxt:
                BusinessLocation.objects.filter(
                    business=instance.business, is_primary=True
                ).update(is_primary=False)
                nxt.is_primary = True
                nxt.save(update_fields=["is_primary", "updated_at"])
                sync_business_profile_from_primary_location(nxt)

        return instance
