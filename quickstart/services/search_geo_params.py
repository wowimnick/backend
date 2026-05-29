"""Shared geo constants for public explore (no imports from public_class_views — avoids cycles)."""

from __future__ import annotations

import math

from django.contrib.gis.geos import Point

from quickstart.constants.explore_location_presets import PRESET_LOCATIONS

DEFAULT_SEARCH_RADIUS_KM = 100
MAX_SEARCH_RADIUS_KM = 500

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
    return normalize_search_radius_km(radius_km_str)


def is_valid_lat_lng(lat: float, lng: float) -> bool:
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def normalize_search_radius_km(radius_km_str: str | None) -> float:
    default = float(DEFAULT_SEARCH_RADIUS_KM)
    if (
        radius_km_str
        and str(radius_km_str).replace(".", "", 1).replace("-", "", 1).isdigit()
    ):
        try:
            radius = float(radius_km_str)
            if math.isfinite(radius) and radius > 0:
                return min(radius, MAX_SEARCH_RADIUS_KM)
        except (TypeError, ValueError):
            pass
    return default


def typesense_geo_radius_clause(lat: float, lng: float, radius_km: float) -> str | None:
    if not is_valid_lat_lng(lat, lng):
        return None
    if not math.isfinite(radius_km) or radius_km <= 0:
        return None
    radius = min(radius_km, MAX_SEARCH_RADIUS_KM)
    return f"location:({lat:.6f}, {lng:.6f}, {radius} km)"


def toronto_gta_typesense_location_clause(radius_km_str: str | None) -> str:
    """
    Typesense geo filter for Toronto/GTA preset searches.

    Uses a fixed radius from downtown Toronto — not the Toronto city polygon —
    so Typesense counts and pagination match Postgres explore (see PublicClassViewSet.search).
    """
    metro_radius_km = toronto_gta_radius_km(radius_km_str)
    c = toronto_gta_center_point()
    clause = typesense_geo_radius_clause(float(c.y), float(c.x), metro_radius_km)
    if clause:
        return clause
    return f"location:({c.y:.6f}, {c.x:.6f}, {DEFAULT_SEARCH_RADIUS_KM} km)"


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
