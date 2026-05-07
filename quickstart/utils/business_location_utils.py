"""Helpers for BusinessLocation <-> BusinessInfo and ClassesMain sync."""

import re

from django.contrib.gis.geos import Point

from quickstart.models import BusinessInfo, BusinessLocation, ClassesMain


def normalize_business_location_fingerprint_tuple(
    address=None,
    unit=None,
    city=None,
    state=None,
    zip_code=None,
):
    """Normalized address components for duplicate detection."""

    def norm(s):
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    z = re.sub(r"\s+", "", (zip_code or "").strip().lower())
    return (
        norm(address or ""),
        norm(unit or ""),
        norm(city or ""),
        norm(state or ""),
        z,
    )


def business_location_identity_key_from_values(
    address=None,
    unit=None,
    city=None,
    state=None,
    zip_code=None,
    latitude=None,
    longitude=None,
):
    """
    Stable identity for duplicate checks: normalized address tuple, or rounded lat/lng
    when there is no usable street line.
    """
    tup = normalize_business_location_fingerprint_tuple(
        address, unit, city, state, zip_code
    )
    if any(tup):
        return ("fields", tup)
    try:
        if latitude is not None and longitude is not None:
            return (
                "ll",
                round(float(latitude), 5),
                round(float(longitude), 5),
            )
    except (TypeError, ValueError):
        pass
    return ("none", None)


def business_location_identity_key_from_instance(loc: BusinessLocation):
    return business_location_identity_key_from_values(
        loc.address,
        loc.unit,
        loc.city,
        loc.state,
        loc.zip_code,
        loc.latitude,
        loc.longitude,
    )


def business_location_identity_key_from_merged(instance, data: dict):
    """Merge serializer `data` with `instance` fields for a partial PATCH identity."""
    if instance is None:
        return business_location_identity_key_from_values(
            data.get("address"),
            data.get("unit"),
            data.get("city"),
            data.get("state"),
            data.get("zip_code"),
            data.get("latitude"),
            data.get("longitude"),
        )

    def pick(field):
        if field in data:
            return data.get(field)
        return getattr(instance, field, None)

    return business_location_identity_key_from_values(
        pick("address"),
        pick("unit"),
        pick("city"),
        pick("state"),
        pick("zip_code"),
        pick("latitude"),
        pick("longitude"),
    )


def business_location_to_class_field_dict(loc):
    """Map a BusinessLocation to ClassesMain location-related fields."""
    coords = ""
    if loc.latitude is not None and loc.longitude is not None:
        coords = f"{float(loc.latitude)},{float(loc.longitude)}"
    elif loc.point:
        coords = f"{loc.point.y},{loc.point.x}"
    return {
        "location": loc.address,
        "unit_number": loc.unit or "",
        "city": loc.city,
        "state": loc.state,
        "coordinates": coords,
        "saltLocation": not loc.show_exact_location,
        "point": loc.point,
    }


def apply_business_location_to_class_instance(instance: ClassesMain, loc: BusinessLocation):
    """Set location_ref and denormalized address fields on a class."""
    data = business_location_to_class_field_dict(loc)
    for key, value in data.items():
        setattr(instance, key, value)
    instance.location_ref = loc


def sync_primary_location_from_business_profile(business: BusinessInfo):
    """
    After BusinessInfo address fields are updated, mirror them onto the primary
    BusinessLocation (create one if missing and address is present).
    """
    addr = (business.businessAddress or "").strip()
    if not addr:
        return

    primary = (
        BusinessLocation.objects.filter(
            business=business, is_primary=True, is_active=True
        )
        .order_by("-updated_at")
        .first()
    )

    pt = None
    if business.latitude is not None and business.longitude is not None:
        pt = Point(float(business.longitude), float(business.latitude), srid=4326)

    if primary:
        primary.address = business.businessAddress
        primary.unit = business.businessUnit or ""
        primary.city = business.businessCity
        primary.state = business.businessState
        primary.zip_code = business.businessZipCode or ""
        primary.latitude = business.latitude
        primary.longitude = business.longitude
        primary.point = pt
        primary.show_exact_location = business.showExactLocation
        primary.save()
        return

    profile_key = business_location_identity_key_from_values(
        business.businessAddress,
        business.businessUnit or "",
        business.businessCity,
        business.businessState,
        business.businessZipCode or "",
        business.latitude,
        business.longitude,
    )
    if profile_key[0] != "none":
        for loc in BusinessLocation.objects.filter(
            business=business, is_active=True
        ).order_by("-is_primary", "name"):
            if business_location_identity_key_from_instance(loc) == profile_key:
                BusinessLocation.objects.filter(
                    business=business, is_primary=True
                ).update(is_primary=False)
                loc.is_primary = True
                loc.address = business.businessAddress
                loc.unit = business.businessUnit or ""
                loc.city = business.businessCity
                loc.state = business.businessState
                loc.zip_code = business.businessZipCode or ""
                loc.latitude = business.latitude
                loc.longitude = business.longitude
                loc.point = pt
                loc.show_exact_location = business.showExactLocation
                loc.save()
                sync_business_profile_from_primary_location(loc)
                return

    BusinessLocation.objects.filter(business=business, is_primary=True).update(
        is_primary=False
    )
    BusinessLocation.objects.create(
        business=business,
        name="Primary location",
        address=business.businessAddress,
        unit=business.businessUnit or "",
        city=business.businessCity,
        state=business.businessState,
        zip_code=business.businessZipCode or "",
        latitude=business.latitude,
        longitude=business.longitude,
        point=pt,
        show_exact_location=business.showExactLocation,
        is_primary=True,
        is_active=True,
    )


def sync_business_profile_from_primary_location(loc: BusinessLocation):
    """When the primary BusinessLocation is saved, update legacy BusinessInfo fields."""
    if not loc.is_primary or not loc.is_active:
        return
    business = loc.business
    business.businessAddress = loc.address
    business.businessUnit = loc.unit or ""
    business.businessCity = loc.city
    business.businessState = loc.state
    business.businessZipCode = loc.zip_code or ""
    business.latitude = loc.latitude
    business.longitude = loc.longitude
    business.showExactLocation = loc.show_exact_location
    business.save(
        update_fields=[
            "businessAddress",
            "businessUnit",
            "businessCity",
            "businessState",
            "businessZipCode",
            "latitude",
            "longitude",
            "showExactLocation",
            "updatedAt",
        ]
    )
