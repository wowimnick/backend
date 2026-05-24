"""Shared geo constants for public explore (no imports from public_class_views — avoids cycles)."""

from __future__ import annotations

from django.contrib.gis.geos import Point

from quickstart.constants.explore_location_presets import PRESET_LOCATIONS

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


def is_toronto_gta_search_name(search_name: str) -> bool:
    if not search_name:
        return False
    n = search_name.strip().lower()
    return n == "toronto" or n.startswith("toronto (")


def toronto_gta_center_point() -> Point:
    lat, lng = PRESET_LOCATIONS["Toronto"]
    return Point(float(lng), float(lat), srid=4326)


def toronto_gta_radius_km(radius_km_str: str | None) -> float:
    """GTA explore radius (matches Postgres public search)."""
    if (
        radius_km_str
        and str(radius_km_str).replace(".", "", 1).replace("-", "", 1).isdigit()
    ):
        return float(radius_km_str)
    return float(DEFAULT_SEARCH_RADIUS_KM)


def toronto_gta_typesense_location_clause(radius_km_str: str | None) -> str:
    """
    Typesense geo filter for Toronto/GTA preset searches.

    Uses a fixed radius from downtown Toronto — not the Toronto city polygon —
    so Typesense counts and pagination match Postgres explore (see PublicClassViewSet.search).
    """
    metro_radius_km = toronto_gta_radius_km(radius_km_str)
    c = toronto_gta_center_point()
    return f"location:({c.y:.6f}, {c.x:.6f}, {metro_radius_km} km)"


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
