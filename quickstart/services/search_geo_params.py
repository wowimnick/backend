"""Shared geo constants for public explore (no imports from public_class_views — avoids cycles)."""

from __future__ import annotations

from django.contrib.gis.geos import Point

DEFAULT_SEARCH_RADIUS_KM = 100

CANADIAN_PROVINCES = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "nova scotia": "NS",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "saskatchewan": "SK",
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
}

PROVINCE_ABBREVIATIONS = {v: k for k, v in CANADIAN_PROVINCES.items()}


def business_states_for_province_field(province_field: str | None) -> list[str]:
    """
    Values aligned with ClassesMain.businessId.businessState and Typesense business_state:
    full province name + postal abbreviation when known (e.g. Ontario + ON).
    """
    if province_field is None:
        return []
    raw = str(province_field).strip()
    if not raw:
        return []
    pl = raw.lower()
    if pl in CANADIAN_PROVINCES:
        abbr = CANADIAN_PROVINCES[pl]
        return [pl.title(), abbr.upper()]
    upper = raw.upper()
    if upper in PROVINCE_ABBREVIATIONS:
        full_key = PROVINCE_ABBREVIATIONS[upper]
        return [full_key.title(), upper]
    return [raw]


PRESET_LOCATIONS = {
    "Toronto": (43.6532, -79.3832),
    "Mississauga": (43.589, -79.6441),
    "Brampton": (43.7315, -79.7624),
    "Vaughan": (43.8367, -79.4982),
    "Markham": (43.8561, -79.337),
    "Richmond Hill": (43.8828, -79.4403),
    "Oakville": (43.4675, -79.6877),
    "Burlington": (43.3255, -79.799),
    "Hamilton": (43.2557, -79.8711),
    "Ottawa": (45.4215, -75.6972),
    "Pickering": (43.8374, -79.0863),
    "Ajax": (43.8501, -79.0329),
    "Whitby": (43.8762, -78.9413),
    "Oshawa": (43.8971, -78.8658),
    "Milton": (43.5183, -79.8774),
    "Newmarket": (44.0553, -79.4593),
    "Aurora": (44.0056, -79.4663),
    "Etobicoke": (43.6532, -79.5672),
    "Scarborough": (43.7731, -79.2574),
    "North York": (43.7615, -79.4111),
}


def is_toronto_gta_search_name(search_name: str) -> bool:
    if not search_name:
        return False
    n = search_name.strip().lower()
    return n == "toronto" or n.startswith("toronto (")


def toronto_gta_center_point() -> Point:
    lat, lng = PRESET_LOCATIONS["Toronto"]
    return Point(float(lng), float(lat), srid=4326)


def normalize_province_name(location_text: str | None) -> str | None:
    if not location_text:
        return None
    cleaned = location_text.replace(", Canada", "").replace(",Canada", "").strip()
    cleaned_lower = cleaned.lower()
    if cleaned.upper() in PROVINCE_ABBREVIATIONS:
        return PROVINCE_ABBREVIATIONS[cleaned.upper()]
    if cleaned_lower in CANADIAN_PROVINCES:
        return cleaned_lower
    return None


def is_preset_location_match(search_name: str | None, lat_str, lng_str) -> bool:
    if not search_name:
        return False
    preset = PRESET_LOCATIONS.get(search_name)
    if not preset:
        return False
    try:
        lat, lng = float(lat_str), float(lng_str)
        return abs(lat - preset[0]) < 0.01 and abs(lng - preset[1]) < 0.01
    except (TypeError, ValueError):
        return False
