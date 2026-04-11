"""Helpers for BusinessLocation <-> BusinessInfo and ClassesMain sync."""

from django.contrib.gis.geos import Point

from quickstart.models import BusinessInfo, BusinessLocation, ClassesMain


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
