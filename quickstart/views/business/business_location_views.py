import logging

from django.db.models import Q
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError as DRFValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from quickstart.models import BusinessInfo, BusinessLocation, ClassesMain
from quickstart.serializers.business.business_location_serializers import (
    BusinessLocationSerializer,
)
from quickstart.utils.permissions import CanManageOwnBusinessProfile

logger = logging.getLogger(__name__)


def get_request_business(user):
    return (
        BusinessInfo.objects.filter(
            Q(owner=user)
            | Q(staff_members__user=user, staff_members__status="accepted")
        )
        .first()
    )


class BusinessLocationListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]
    serializer_class = BusinessLocationSerializer

    def get_queryset(self):
        business = get_request_business(self.request.user)
        if not business:
            return BusinessLocation.objects.none()
        return BusinessLocation.objects.filter(business=business).order_by(
            "-is_primary", "name"
        )

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        business = get_request_business(self.request.user)
        ctx["business"] = business
        return ctx

    def perform_create(self, serializer):
        business = get_request_business(self.request.user)
        if not business:
            raise NotFound("No business profile associated with this user.")
        serializer.save(business=business)  # merged into validated_data for create()


class BusinessLocationDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated, CanManageOwnBusinessProfile]
    serializer_class = BusinessLocationSerializer
    lookup_field = "pk"

    def get_queryset(self):
        business = get_request_business(self.request.user)
        if not business:
            return BusinessLocation.objects.none()
        return BusinessLocation.objects.filter(business=business)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        business = get_request_business(self.request.user)
        ctx["business"] = business
        return ctx

    def perform_destroy(self, instance):
        assigned = ClassesMain.objects.filter(location_ref=instance).count()
        if assigned > 0:
            instance.is_active = False
            instance.save(update_fields=["is_active", "updated_at"])
            if instance.is_primary:
                other = (
                    BusinessLocation.objects.filter(
                        business=instance.business, is_active=True
                    )
                    .exclude(pk=instance.pk)
                    .order_by("-is_primary", "name")
                    .first()
                )
                if other:
                    BusinessLocation.objects.filter(
                        business=instance.business, is_primary=True
                    ).update(is_primary=False)
                    other.is_primary = True
                    other.save(update_fields=["is_primary", "updated_at"])
                    from quickstart.utils.business_location_utils import (
                        sync_business_profile_from_primary_location,
                    )

                    sync_business_profile_from_primary_location(other)
            return

        if instance.is_primary:
            other = (
                BusinessLocation.objects.filter(
                    business=instance.business, is_active=True
                )
                .exclude(pk=instance.pk)
                .order_by("name")
                .first()
            )
            if other:
                BusinessLocation.objects.filter(
                    business=instance.business, is_primary=True
                ).update(is_primary=False)
                other.is_primary = True
                other.save(update_fields=["is_primary", "updated_at"])
                from quickstart.utils.business_location_utils import (
                    sync_business_profile_from_primary_location,
                )

                sync_business_profile_from_primary_location(other)
            else:
                raise DRFValidationError(
                    {
                        "detail": "Cannot delete the only location. Add another location first or deactivate this one."
                    }
                )

        instance.delete()

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        assigned = ClassesMain.objects.filter(location_ref=instance).count()
        self.perform_destroy(instance)
        if assigned > 0:
            return Response(
                {
                    "detail": "Location has classes assigned; it was deactivated instead of deleted.",
                    "deactivated": True,
                },
                status=status.HTTP_200_OK,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
