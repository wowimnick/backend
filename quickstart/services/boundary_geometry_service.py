"""
Buffered geographic boundaries for Typesense polygon filters.
Coordinates are lat,lng pairs suitable for Typesense filter_by location:(lat,lng,...).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db import connection

logger = logging.getLogger(__name__)

BOUNDARY_CACHE_PREFIX = "boundary_buffered_polygon"


def boundary_polygon_cache_key(boundary_id: UUID | str) -> str:
    buf_m = getattr(settings, "GEO_BOUNDARY_BUFFER_METERS", 5000)
    tol = getattr(settings, "GEO_BOUNDARY_SIMPLIFY_TOLERANCE", 0.00005)
    return f"{BOUNDARY_CACHE_PREFIX}:v2:{buf_m}:{tol}:{boundary_id}"


def fetch_buffered_polygon_latlng_coords_sql(
    boundary_id: UUID | str,
    buffer_meters: int | None = None,
    simplify_tolerance: float | None = None,
) -> list[tuple[float, float]] | None:
    """Return exterior ring as [(lat, lng), ...] from PostGIS."""
    buffer_m = buffer_meters if buffer_meters is not None else getattr(
        settings, "GEO_BOUNDARY_BUFFER_METERS", 5000
    )
    tol = simplify_tolerance if simplify_tolerance is not None else getattr(
        settings, "GEO_BOUNDARY_SIMPLIFY_TOLERANCE", 0.00005
    )
    bid = str(boundary_id)
    sql = """
        WITH src AS (
          SELECT geom FROM quickstart_geographicboundary WHERE id = %s::uuid
        ),
        buffered AS (
          SELECT ST_Buffer(geom::geography, %s)::geometry AS g FROM src
        ),
        dumped AS (
          SELECT (ST_Dump(g)).geom AS poly FROM buffered
        ),
        largest AS (
          SELECT poly FROM dumped ORDER BY ST_Area(poly::geography) DESC LIMIT 1
        ),
        simplified AS (
          SELECT ST_Simplify(poly, %s) AS poly FROM largest
        )
        SELECT ST_AsGeoJSON(poly) FROM simplified
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [bid, buffer_m, tol])
        row = cursor.fetchone()
    if not row or not row[0]:
        return None
    gj = json.loads(row[0])
    coords = _coords_from_geojson_polygon(gj)
    return coords


def _coords_from_geojson_polygon(gj: dict[str, Any]) -> list[tuple[float, float]] | None:
    t = gj.get("type")
    if t == "Polygon":
        rings = gj.get("coordinates") or []
        if not rings:
            return None
        ring = rings[0]
        return _ring_latlng(ring)
    if t == "MultiPolygon":
        polys = gj.get("coordinates") or []
        best = None
        best_area = -1.0
        for poly in polys:
            if not poly:
                continue
            ring = poly[0]
            pts = len(ring)
            if pts > best_area:
                best_area = float(pts)
                best = ring
        return _ring_latlng(best) if best else None
    return None


def _ring_latlng(ring: list) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for pair in ring:
        if not pair or len(pair) < 2:
            continue
        lng, lat = float(pair[0]), float(pair[1])
        out.append((lat, lng))
    if len(out) >= 2 and out[0] == out[-1]:
        out = out[:-1]
    return out


def get_cached_boundary_polygon_coords(
    boundary_id: UUID | str,
) -> list[tuple[float, float]] | None:
    key = boundary_polygon_cache_key(boundary_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    coords = fetch_buffered_polygon_latlng_coords_sql(boundary_id)
    if coords:
        cache.set(
            key,
            coords,
            timeout=getattr(settings, "BOUNDARY_POLYGON_CACHE_SECONDS", 604800),
        )
    return coords


def rebuild_boundary_polygon_cache(boundary_id: UUID | str) -> None:
    key = boundary_polygon_cache_key(boundary_id)
    cache.delete(key)
    coords = fetch_buffered_polygon_latlng_coords_sql(boundary_id)
    if coords:
        cache.set(
            key,
            coords,
            timeout=getattr(settings, "BOUNDARY_POLYGON_CACHE_SECONDS", 604800),
        )


def rebuild_all_boundary_polygon_caches() -> int:
    from quickstart.models import GeographicBoundary

    count = 0
    for bid in GeographicBoundary.objects.values_list("id", flat=True):
        rebuild_boundary_polygon_cache(bid)
        count += 1
    logger.info("Rebuilt buffered polygon cache for %s geographic boundaries", count)
    return count


def format_typesense_polygon_filter(coords: list[tuple[float, float]]) -> str | None:
    from quickstart.services.search_geo_params import is_valid_lat_lng

    parts: list[str] = []
    for lat, lng in coords:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            continue
        if not is_valid_lat_lng(lat_f, lng_f):
            continue
        parts.append(f"{lat_f:.7f},{lng_f:.7f}")
    if len(parts) < 3:
        return None
    return "(" + ", ".join(parts) + ")"
