# Data migration: BusinessLocation rows from BusinessInfo + class addresses, link ClassesMain.location_ref

from django.db import migrations


def _norm_key(address, city, state, unit):
    return (
        (address or "").strip().lower(),
        (city or "").strip().lower(),
        (state or "").strip().lower(),
        (unit or "").strip().lower(),
    )


def migrate_forwards(apps, schema_editor):
    BusinessInfo = apps.get_model("quickstart", "BusinessInfo")
    BusinessLocation = apps.get_model("quickstart", "BusinessLocation")
    ClassesMain = apps.get_model("quickstart", "ClassesMain")

    try:
        from django.contrib.gis.geos import Point
    except Exception:  # pragma: no cover
        Point = None

    loc_map = {}  # (business_id, key) -> BusinessLocation pk (uuid str)
    has_primary = {}

    for biz in BusinessInfo.objects.all().iterator():
        bid = biz.businessId
        addr = (biz.businessAddress or "").strip()
        has_primary[bid] = bool(addr)
        if not addr:
            continue
        pt = None
        if (
            Point is not None
            and biz.latitude is not None
            and biz.longitude is not None
        ):
            pt = Point(float(biz.longitude), float(biz.latitude), srid=4326)
        loc = BusinessLocation.objects.create(
            business_id=bid,
            name="Primary location",
            address=biz.businessAddress,
            unit=biz.businessUnit or "",
            city=biz.businessCity,
            state=biz.businessState,
            zip_code=biz.businessZipCode,
            latitude=biz.latitude,
            longitude=biz.longitude,
            point=pt,
            show_exact_location=biz.showExactLocation,
            is_primary=True,
            is_active=True,
        )
        key = _norm_key(
            biz.businessAddress,
            biz.businessCity,
            biz.businessState,
            biz.businessUnit,
        )
        loc_map[(bid, key)] = loc.id

    for cls in (
        ClassesMain.objects.select_related("businessId")
        .all()
        .iterator(chunk_size=500)
    ):
        bid = cls.businessId_id
        key = _norm_key(cls.location, cls.city, cls.state, cls.unit_number)
        if (bid, key) in loc_map:
            continue

        is_prim = not has_primary.get(bid, False)
        if is_prim:
            has_primary[bid] = True

        pt = None
        if Point is not None and cls.point is not None:
            pt = cls.point

        name_base = (cls.city or "").strip() or (cls.location or "")[:80] or "Class location"
        name = (name_base[:147] + "...") if len(name_base) > 150 else name_base
        if len(name) < 2:
            name = "Class location"

        loc = BusinessLocation.objects.create(
            business_id=bid,
            name=name,
            address=cls.location,
            unit=cls.unit_number or "",
            city=cls.city or "",
            state=cls.state or "",
            zip_code="",
            latitude=None,
            longitude=None,
            point=pt,
            show_exact_location=not cls.saltLocation,
            is_primary=is_prim,
            is_active=True,
        )
        loc_map[(bid, key)] = loc.id

    for cls in ClassesMain.objects.all().iterator(chunk_size=500):
        bid = cls.businessId_id
        key = _norm_key(cls.location, cls.city, cls.state, cls.unit_number)
        loc_id = loc_map.get((bid, key))
        if loc_id:
            cls.location_ref_id = loc_id
            cls.save(update_fields=["location_ref_id"])


def migrate_backwards(apps, schema_editor):
    ClassesMain = apps.get_model("quickstart", "ClassesMain")
    BusinessLocation = apps.get_model("quickstart", "BusinessLocation")
    ClassesMain.objects.all().update(location_ref=None)
    BusinessLocation.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0224_business_location_and_class_fk"),
    ]

    operations = [
        migrations.RunPython(migrate_forwards, migrate_backwards),
    ]
