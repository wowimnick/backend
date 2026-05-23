"""
Explore location preset catalog — census-boundary cities only (no pseudo-neighborhoods).

Keep displayName values in sync with classeasily-frontend-next location preset exports.
"""

from __future__ import annotations

from typing import Any

# Ordered catalog: Toronto first, then GTA peers, then wider Ontario.
EXPLORE_LOCATION_PRESET_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "name": "Toronto",
        "displayName": "Toronto, ON",
        "description": "Downtown & neighbourhoods",
        "coords": {"lat": 43.6532, "lng": -79.3832},
        "provinceSlug": "ontario",
        "citySlug": "toronto",
    },
    {
        "name": "Mississauga",
        "displayName": "Mississauga, ON",
        "description": "West of Toronto",
        "coords": {"lat": 43.589, "lng": -79.6441},
        "provinceSlug": "ontario",
        "citySlug": "mississauga",
    },
    {
        "name": "Markham",
        "displayName": "Markham, ON",
        "description": "York Region",
        "coords": {"lat": 43.8561, "lng": -79.337},
        "provinceSlug": "ontario",
        "citySlug": "markham",
    },
    {
        "name": "Vaughan",
        "displayName": "Vaughan, ON",
        "description": "North of Toronto",
        "coords": {"lat": 43.8367, "lng": -79.4982},
        "provinceSlug": "ontario",
        "citySlug": "vaughan",
    },
    {
        "name": "Oakville",
        "displayName": "Oakville, ON",
        "description": "Halton Region",
        "coords": {"lat": 43.4675, "lng": -79.6877},
        "provinceSlug": "ontario",
        "citySlug": "oakville",
    },
    {
        "name": "Burlington",
        "displayName": "Burlington, ON",
        "description": "Halton Region",
        "coords": {"lat": 43.3255, "lng": -79.799},
        "provinceSlug": "ontario",
        "citySlug": "burlington",
    },
    {
        "name": "Hamilton",
        "displayName": "Hamilton, ON",
        "description": "Golden Horseshoe",
        "coords": {"lat": 43.2557, "lng": -79.8711},
        "provinceSlug": "ontario",
        "citySlug": "hamilton",
    },
    {
        "name": "Brampton",
        "displayName": "Brampton, ON",
        "description": "Peel Region",
        "coords": {"lat": 43.7315, "lng": -79.7624},
        "provinceSlug": "ontario",
        "citySlug": "brampton",
    },
    {
        "name": "Richmond Hill",
        "displayName": "Richmond Hill, ON",
        "description": "York Region",
        "coords": {"lat": 43.8828, "lng": -79.4403},
        "provinceSlug": "ontario",
        "citySlug": "richmond-hill",
    },
    {
        "name": "Pickering",
        "displayName": "Pickering, ON",
        "description": "Durham Region",
        "coords": {"lat": 43.8374, "lng": -79.0863},
        "provinceSlug": "ontario",
        "citySlug": "pickering",
    },
    {
        "name": "Ajax",
        "displayName": "Ajax, ON",
        "description": "Durham Region",
        "coords": {"lat": 43.8501, "lng": -79.0329},
        "provinceSlug": "ontario",
        "citySlug": "ajax",
    },
    {
        "name": "Whitby",
        "displayName": "Whitby, ON",
        "description": "Durham Region",
        "coords": {"lat": 43.8762, "lng": -78.9413},
        "provinceSlug": "ontario",
        "citySlug": "whitby",
    },
    {
        "name": "Oshawa",
        "displayName": "Oshawa, ON",
        "description": "Durham Region",
        "coords": {"lat": 43.8971, "lng": -78.8658},
        "provinceSlug": "ontario",
        "citySlug": "oshawa",
    },
    {
        "name": "Milton",
        "displayName": "Milton, ON",
        "description": "Halton Region",
        "coords": {"lat": 43.5183, "lng": -79.8774},
        "provinceSlug": "ontario",
        "citySlug": "milton",
    },
    {
        "name": "Newmarket",
        "displayName": "Newmarket, ON",
        "description": "York Region",
        "coords": {"lat": 44.0553, "lng": -79.4593},
        "provinceSlug": "ontario",
        "citySlug": "newmarket",
    },
    {
        "name": "Aurora",
        "displayName": "Aurora, ON",
        "description": "York Region",
        "coords": {"lat": 44.0056, "lng": -79.4663},
        "provinceSlug": "ontario",
        "citySlug": "aurora",
    },
    {
        "name": "Ottawa",
        "displayName": "Ottawa, ON",
        "description": "National capital region",
        "coords": {"lat": 45.4215, "lng": -75.6972},
        "provinceSlug": "ontario",
        "citySlug": "ottawa",
    },
)

PRESET_LOCATIONS: dict[str, tuple[float, float]] = {
    p["name"]: (float(p["coords"]["lat"]), float(p["coords"]["lng"]))
    for p in EXPLORE_LOCATION_PRESET_CATALOG
}

EXPLORE_LOCATION_PRESET_LABELS: tuple[str, ...] = (
    "Anywhere",
    *(p["displayName"] for p in EXPLORE_LOCATION_PRESET_CATALOG),
)
