"""
Typesense-backed public class search (mirrors PublicClassViewSet.search semantics).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from quickstart.models import ClassesMain, GeographicBoundary, ScheduleInstance
from quickstart.services.boundary_geometry_service import (
    format_typesense_polygon_filter,
    get_cached_boundary_polygon_coords,
)
from quickstart.services.search_geo_params import (
    CANADIAN_PROVINCES,
    DEFAULT_SEARCH_RADIUS_KM,
    is_toronto_gta_search_name,
    normalize_province_name,
    toronto_gta_center_point,
)
from quickstart.services.search_filter_params import normalize_booking_type_query
from quickstart.services.search_index_service import (
    AVAILABILITY_SLOT_SEP,
    WEEKDAY_ABBR,
    date_to_yyyymmdd,
)
from quickstart.services.search_suggest_service import match_collection_by_alias
from quickstart.services.typesense_client import get_typesense_client
from quickstart.utils.url_utils import build_cloudfront_resized_webp_from_original_key

logger = logging.getLogger(__name__)

_TIME_PREFS = {
    "Morning (6am-12pm)",
    "Afternoon (12pm-5pm)",
    "Evening (5pm-10pm)",
}

# Precise availability_slots filters explode when the calendar span × prefs is large (e.g.
# time-of-day only ⇒ ~365 days × 3 buckets). Fall back to available_dates && time_buckets.
_AVAIL_SLOT_MAX_CALENDAR_DAYS = 62
_AVAIL_SLOT_MAX_PAIRS = 240


def _hydrate_card_row_image_medium_urls(row: dict) -> None:
    """
    Typesense card_json stores medium_url at index time; if the index was built
    without CLOUDFRONT_DOMAIN (or keys changed), URLs are null while DB search
    still resolves them. Rebuild medium_url from image_key at response time.
    Uses copied image dicts so cached payloads are not mutated in place.
    """
    imgs = row.get("images")
    if not imgs:
        return
    out_imgs = []
    for im in imgs:
        if not isinstance(im, dict):
            out_imgs.append(im)
            continue
        d = dict(im)
        if not d.get("medium_url"):
            url = build_cloudfront_resized_webp_from_original_key(d.get("image_key"), "medium")
            if url:
                d["medium_url"] = url
        out_imgs.append(d)
    row["images"] = out_imgs


def _collection_alias_name() -> str:
    return getattr(settings, "TYPESENSE_COLLECTION_ALIAS", "classes_live")


def _physical_collection_name(client) -> str:
    alias = _collection_alias_name()
    try:
        return client.aliases[alias].retrieve().get("collection_name") or alias
    except Exception:
        try:
            client.collections[alias].retrieve()
            return alias
        except Exception:
            return alias


def _canonical_cache_key(request) -> str:
    items = []
    for k in sorted(request.query_params.keys()):
        for v in request.query_params.getlist(k):
            items.append(f"{k}={v}")
    raw = "|".join(items)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_page_url(request, page: int) -> str:
    q = request.GET.copy()
    q["page"] = str(page)
    return request.build_absolute_uri(request.path) + "?" + q.urlencode()


def _classes_queryset_collections_and_sub(
    effective_collection_slugs: list[str],
    sub_slugs: list[str],
    booking_type: str | None = None,
):
    """Same collection/sub/booking filtering Postgres applies before named-boundary geo."""
    today = timezone.now().date()
    qs = (
        ClassesMain.objects.filter(
            status="active",
            businessId__isActive=True,
            businessId__verificationStatus="verified",
        )
        .filter(
            Exists(
                ScheduleInstance.objects.filter(
                    schedule__option__classId=OuterRef("pk"),
                    date__gte=today,
                    status="scheduled",
                )
            )
        )
    )
    if effective_collection_slugs:
        if len(effective_collection_slugs) == 1:
            qs = qs.filter(collections__slug=effective_collection_slugs[0])
        else:
            qs = qs.filter(collections__slug__in=effective_collection_slugs)
        qs = qs.distinct()
    if sub_slugs:
        qs = qs.filter(collections__slug__in=sub_slugs).distinct()
    if booking_type:
        qs = qs.filter(options__booking_type=booking_type).distinct()
    return qs


def _facet_or(field: str, values: list[str]) -> str | None:
    if not values:
        return None
    parts = []
    for v in values:
        escaped = str(v).replace("`", "\\`")
        parts.append(f"{field}:=`{escaped}`")
    return "(" + " || ".join(parts) + ")"


# Typesense rejects GET search URLs longer than ~4000 chars (large polygon filter_by).
_TYPESENSE_GET_QUERY_SAFE_MAX = 3800


def _typesense_search_collection(client, coll: str, search_params: dict[str, Any]) -> dict[str, Any]:
    """Run a collection search via GET or POST multi_search so huge filter_by (polygons) works."""
    from typesense.preprocess import stringify_search_params
    from typesense.validation import validate_search
    from urllib.parse import urlencode

    stringified = stringify_search_params(search_params)
    validate_search(stringified)
    qs_len = len(urlencode(sorted(stringified.items())))
    if qs_len <= _TYPESENSE_GET_QUERY_SAFE_MAX:
        return client.collections[coll].documents.search(search_params)

    wrapped = client.multi_search.perform(
        {"searches": [{"collection": coll, **stringified}]},
        {},
    )
    results = wrapped.get("results") or []
    if not results:
        return {"hits": [], "found": 0}
    return results[0]


def run_public_class_search(request, favorited_ids: set | None = None) -> dict[str, Any]:
    favorited_ids = favorited_ids or set()
    client = get_typesense_client()
    if not client:
        raise RuntimeError("Typesense is not configured")

    ttl = getattr(settings, "SEARCH_RESULTS_CACHE_SECONDS", 10)
    if ttl > 0:
        ck = "typesense_search:" + _canonical_cache_key(request)
        hit = cache.get(ck)
        if hit is not None:
            return _patch_favorites(hit, favorited_ids)

    qp = request.query_params
    req_lat_str = qp.get("lat")
    req_lng_str = qp.get("lng")
    location_param_text = qp.get("location") or qp.get("location_search") or ""
    search_name = location_param_text.split(",")[0].strip()

    raw_collections = []
    seen = set()
    for s in qp.getlist("collection"):
        if not s:
            continue
        t = str(s).strip()
        if not t:
            continue
        k = t.lower()
        if k not in seen:
            seen.add(k)
            raw_collections.append(t)

    keyword_query_text = qp.get("keyword")
    effective_collection_slugs = list(raw_collections)
    resolved_collection_meta = None
    if keyword_query_text and not raw_collections:
        matched = match_collection_by_alias(keyword_query_text.strip())
        if matched:
            effective_collection_slugs = [matched.slug]
            resolved_collection_meta = {"slug": matched.slug, "name": matched.name}

    tag_filter = qp.get("tag")
    price_min_str = qp.get("price_min")
    price_max_str = qp.get("price_max")
    req_radius_km_str = qp.get("radius")
    req_date_str = qp.get("date")
    req_start_date_str = qp.get("start_date")
    req_end_date_str = qp.get("end_date")
    req_participants_str = qp.get("participants")
    time_preferences = qp.getlist("time_preference")
    sort_by = qp.get("sort_by", "relevance")
    days_params = [str(d).strip() for d in qp.getlist("days") if str(d).strip()]
    sub_slugs = [s.strip() for s in qp.getlist("sub") if s and str(s).strip()]
    bt_norm = normalize_booking_type_query(qp.get("class_type"))

    page = int(qp.get("page", "1") or 1)
    page_size = min(int(qp.get("page_size", "24") or 24), 100)
    count_only = str(qp.get("count_only", "")).lower() in ("1", "true", "yes")
    if count_only:
        page = 1
        page_size = 1

    filter_parts: list[str] = []

    normalized_province = normalize_province_name(location_param_text)
    sort_lat = sort_lng = None
    try:
        if req_lat_str and req_lng_str:
            sort_lat = float(req_lat_str)
            sort_lng = float(req_lng_str)
    except (TypeError, ValueError):
        sort_lat = sort_lng = None

    metro_handled = False
    if normalized_province:
        prov_full = normalized_province.title()
        prov_abbr = CANADIAN_PROVINCES.get(normalized_province, "").upper()
        states = [prov_full]
        if prov_abbr:
            states.append(prov_abbr)
        fo = _facet_or("business_state", states)
        if fo:
            filter_parts.append(fo)
    elif search_name and is_toronto_gta_search_name(search_name):
        metro_handled = True
        tb = GeographicBoundary.objects.filter(name__iexact="Toronto").first()
        coords = get_cached_boundary_polygon_coords(tb.id) if tb else None
        if coords:
            filter_parts.append("location:" + format_typesense_polygon_filter(coords))
        else:
            metro_radius_km = (
                float(req_radius_km_str)
                if req_radius_km_str
                and req_radius_km_str.replace(".", "", 1).replace("-", "", 1).isdigit()
                else float(DEFAULT_SEARCH_RADIUS_KM)
            )
            c = toronto_gta_center_point()
            filter_parts.append(
                f"location:({c.y:.6f}, {c.x:.6f}, {metro_radius_km} km)"
            )
        if sort_lat is None:
            c = toronto_gta_center_point()
            sort_lat, sort_lng = float(c.y), float(c.x)
    elif search_name:
        boundary = GeographicBoundary.objects.filter(
            Q(name__iexact=search_name) | Q(name__istartswith=f"{search_name} (")
        ).first()
        if boundary:
            metro_handled = True
            nearby_radius_km = (
                float(req_radius_km_str)
                if req_radius_km_str
                and req_radius_km_str.replace(".", "", 1).replace("-", "", 1).isdigit()
                else float(DEFAULT_SEARCH_RADIUS_KM)
            )
            centroid = boundary.geom.centroid
            coords = get_cached_boundary_polygon_coords(boundary.id)
            used_polygon = False
            if coords:
                qs_chk = _classes_queryset_collections_and_sub(
                    effective_collection_slugs,
                    sub_slugs,
                    booking_type=bt_norm,
                )
                if qs_chk.filter(point__within=boundary.geom).exists():
                    filter_parts.append(
                        "location:" + format_typesense_polygon_filter(coords)
                    )
                    used_polygon = True
                else:
                    logger.info(
                        "Typesense: no classes in raw boundary %r; centroid+radius %.1fkm (Postgres parity).",
                        boundary.name,
                        nearby_radius_km,
                    )
                    filter_parts.append(
                        f"location:({centroid.y:.6f}, {centroid.x:.6f}, {nearby_radius_km} km)"
                    )
            else:
                filter_parts.append(
                    f"location:({centroid.y:.6f}, {centroid.x:.6f}, {nearby_radius_km} km)"
                )
            if sort_lat is None and centroid and not used_polygon:
                sort_lat, sort_lng = float(centroid.y), float(centroid.x)

    if (
        not metro_handled
        and not normalized_province
        and req_lat_str
        and req_lng_str
    ):
        try:
            lat_u = float(req_lat_str)
            lng_u = float(req_lng_str)
            search_radius_km = (
                float(req_radius_km_str)
                if req_radius_km_str
                and req_radius_km_str.replace(".", "", 1).replace("-", "", 1).isdigit()
                else float(DEFAULT_SEARCH_RADIUS_KM)
            )
            filter_parts.append(
                f"location:({lat_u:.6f}, {lng_u:.6f}, {search_radius_km} km)"
            )
            sort_lat = lat_u
            sort_lng = lng_u
        except (TypeError, ValueError):
            pass

    if effective_collection_slugs:
        fo = _facet_or("collection_slugs", effective_collection_slugs)
        if fo:
            filter_parts.append(fo)

    if sub_slugs:
        paths_clause = _facet_or("collection_paths", sub_slugs)
        slug_clause = _facet_or("collection_slugs", sub_slugs)
        if paths_clause and slug_clause:
            filter_parts.append(f"({paths_clause} || {slug_clause})")
        elif paths_clause:
            filter_parts.append(paths_clause)
        elif slug_clause:
            filter_parts.append(slug_clause)

    if tag_filter:
        escaped = str(tag_filter).lower().replace("`", "\\`")
        filter_parts.append(f"option_tags:=`{escaped}`")

    if bt_norm:
        escaped_bt = bt_norm.replace("`", "\\`")
        filter_parts.append(f"booking_types:=`{escaped_bt}`")

    if price_min_str:
        try:
            pmin = Decimal(price_min_str)
            if pmin > 0:
                filter_parts.append(f"min_price:>={float(pmin)}")
        except (InvalidOperation, ValueError):
            logger.warning("Invalid price_min for Typesense search: %s", price_min_str)

    if price_max_str:
        try:
            px = Decimal(price_max_str)
            filter_parts.append(f"(price_unknown:=true || min_price:<={float(px)})")
        except (InvalidOperation, ValueError):
            logger.warning("Invalid price_max for Typesense search: %s", price_max_str)

    # Calendar window only when the client sends explicit date params — not when only
    # time_preference is set (pairing prefs with a synthetic 365-day OR on available_dates
    # broke far-future classes and was slow).
    apply_dates = bool(req_date_str or req_start_date_str or req_end_date_str)
    days_list: list[str] = []
    if apply_dates:
        today = timezone.now().date()
        if req_start_date_str and req_end_date_str:
            try:
                sd = datetime.strptime(req_start_date_str, "%Y-%m-%d").date()
                ed = datetime.strptime(req_end_date_str, "%Y-%m-%d").date()
            except ValueError:
                sd = today
                ed = today + timedelta(days=365)
        elif req_date_str:
            try:
                one = datetime.strptime(req_date_str, "%Y-%m-%d").date()
                sd = ed = one
            except ValueError:
                sd = today
                ed = today + timedelta(days=365)
        else:
            # Partial/malformed date params (e.g. only start_date): use same wide default
            # as legacy behaviour for date filtering.
            sd = today
            ed = today + timedelta(days=365)
        cur = sd
        safety = 0
        while cur <= ed and safety < 400:
            days_list.append(cur.isoformat())
            cur += timedelta(days=1)
            safety += 1

    weekday_requested = [str(d).strip() for d in days_params if str(d).strip()]
    availability_impossible = False
    if apply_dates and weekday_requested:
        want = set(weekday_requested)
        narrowed = []
        for d_str in days_list:
            wd = WEEKDAY_ABBR[datetime.strptime(d_str, "%Y-%m-%d").date().weekday()]
            if wd in want:
                narrowed.append(d_str)
        days_list = narrowed
        if not days_list:
            availability_impossible = True

    prefs = [p for p in time_preferences if p in _TIME_PREFS]

    if availability_impossible:
        filter_parts.append("class_id:<0")
    elif days_list and prefs:
        pair_count = len(days_list) * len(prefs)
        use_precise_slots = (
            len(days_list) <= _AVAIL_SLOT_MAX_CALENDAR_DAYS
            and pair_count <= _AVAIL_SLOT_MAX_PAIRS
        )
        if use_precise_slots:
            combo_values = [
                f"{d}{AVAILABILITY_SLOT_SEP}{p}" for d in days_list for p in prefs
            ]
            slot_fo = _facet_or("availability_slots", combo_values)
            if slot_fo:
                filter_parts.append(slot_fo)
        else:
            date_fo = _facet_or("available_dates", days_list)
            tb_fo = _facet_or("time_buckets", prefs)
            if date_fo:
                filter_parts.append(date_fo)
            if tb_fo:
                filter_parts.append(tb_fo)
    elif days_list:
        date_fo = _facet_or("available_dates", days_list)
        if date_fo:
            filter_parts.append(date_fo)
    elif prefs:
        tb_fo = _facet_or("time_buckets", prefs)
        if tb_fo:
            filter_parts.append(tb_fo)

    # Hide stale index rows (no Typesense range filter on string[]; use int YYYYMMDD).
    filter_parts.append(
        f"max_available_date:>={date_to_yyyymmdd(timezone.now().date())}"
    )

    if (
        req_participants_str
        and str(req_participants_str).isdigit()
        and int(req_participants_str) > 0
    ):
        filter_parts.append(f"max_capacity:>={int(req_participants_str)}")

    if weekday_requested and not apply_dates:
        wd_fo = _facet_or("weekdays", weekday_requested)
        if wd_fo:
            filter_parts.append(wd_fo)

    filter_by = " && ".join(filter_parts) if filter_parts else ""

    qtext = (keyword_query_text or "").strip()
    if keyword_query_text and effective_collection_slugs and resolved_collection_meta is None:
        qtext = ""

    search_params: dict[str, Any] = {
        "q": qtext or "*",
        "query_by": "title,description,business_name,option_tags,collection_slugs,collection_paths",
        "per_page": page_size,
        "page": page,
    }
    if count_only:
        search_params["exclude_fields"] = "card_json"
    if filter_by:
        search_params["filter_by"] = filter_by

    if sort_by == "distance" and sort_lat is not None and sort_lng is not None:
        search_params[
            "sort_by"
        ] = f"location({sort_lat:.6f},{sort_lng:.6f}):asc,class_id:desc"
    elif sort_by == "price_asc":
        search_params["sort_by"] = "min_price:asc,class_id:desc"
    elif sort_by == "price_desc":
        search_params["sort_by"] = "min_price:desc,class_id:desc"
    elif sort_by == "rating":
        search_params["sort_by"] = (
            "combined_avg_rating:desc,combined_review_count_raw:desc,class_id:desc"
        )
    elif sort_by == "reviews":
        search_params["sort_by"] = (
            "combined_review_count_raw:desc,combined_avg_rating:desc,class_id:desc"
        )
    elif sort_by == "newest":
        search_params["sort_by"] = "created_at_ts:desc,class_id:desc"
    else:
        if qtext:
            # Typesense allows at most 3 sort fields (extra fields return 400).
            search_params["sort_by"] = (
                "_text_match:desc,relevance_score:desc,class_id:desc"
            )
        else:
            search_params["sort_by"] = (
                "relevance_score:desc,created_at_ts:desc,class_id:desc"
            )

    coll = _physical_collection_name(client)
    try:
        result = _typesense_search_collection(client, coll, search_params)
    except Exception as exc:
        _name = type(exc).__name__
        if _name != "ObjectNotFound":
            raise
        from quickstart.services.search_index_service import (
            needs_typesense_full_reindex,
            sync_typesense_bootstrap_at_web_startup,
        )

        need_heal, heal_reason = needs_typesense_full_reindex()
        if not need_heal:
            raise
        logger.warning(
            "Typesense search got missing collection; healing (%s)", heal_reason
        )
        sync_typesense_bootstrap_at_web_startup(max_wait_peer_seconds=900)
        coll = _physical_collection_name(client)
        result = _typesense_search_collection(client, coll, search_params)

    hits = result.get("hits") or []
    found = int(result.get("found") or 0)
    results = []
    for h in hits:
        doc = h.get("document") or {}
        raw = doc.get("card_json")
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cid = row.get("classId")
        row["is_favorited"] = cid in favorited_ids if cid is not None else False
        _hydrate_card_row_image_medium_urls(row)
        results.append(row)

    total_pages = (found + page_size - 1) // page_size if found else 1
    prev_url = _build_page_url(request, page - 1) if page > 1 else None
    next_url = _build_page_url(request, page + 1) if page < total_pages else None

    payload = {
        "count": found,
        "next": next_url,
        "previous": prev_url,
        "results": results,
    }
    if resolved_collection_meta:
        payload["resolved_collection"] = resolved_collection_meta

    if ttl > 0:
        cache.set(
            "typesense_search:" + _canonical_cache_key(request),
            json.loads(json.dumps(payload)),
            ttl,
        )
    return payload


def _patch_favorites(payload: dict, favorited_ids: set) -> dict:
    out = dict(payload)
    rows = []
    for row in out.get("results") or []:
        r = dict(row)
        cid = r.get("classId")
        r["is_favorited"] = cid in favorited_ids if cid is not None else False
        _hydrate_card_row_image_medium_urls(r)
        rows.append(r)
    out["results"] = rows
    return out


def normalize_db_search_payload(db_resp: Any) -> dict[str, Any]:
    """Normalize DRF Response.data into {count, next, previous, results}."""
    data = getattr(db_resp, "data", db_resp)
    if isinstance(data, list):
        return {"count": len(data), "next": None, "previous": None, "results": data}
    if isinstance(data, dict):
        if "results" in data:
            return data
        count_only = data.get("count")
        if count_only is not None:
            return {
                "count": count_only,
                "next": data.get("next"),
                "previous": data.get("previous"),
                "results": data.get("results") or [],
            }
        return data
    return {"count": 0, "next": None, "previous": None, "results": []}


def log_search_shadow_diff(ts_payload: dict, db_payload: dict, params: dict) -> None:
    try:
        if int(ts_payload.get("count") or -1) != int(db_payload.get("count") or -2):
            logger.warning(
                "search_shadow count mismatch ts=%s db=%s params=%s",
                ts_payload.get("count"),
                db_payload.get("count"),
                params,
            )
        ts_ids = [r.get("classId") for r in ts_payload.get("results") or []]
        db_ids = [r.get("classId") for r in db_payload.get("results") or []]
        if ts_ids != db_ids:
            logger.warning(
                "search_shadow page mismatch ts_ids=%s db_ids=%s params=%s",
                ts_ids[:10],
                db_ids[:10],
                params,
            )
    except Exception as e:
        logger.debug("search_shadow diff skipped: %s", e)


def compare_shadow_with_db(
    db_builder,
    request,
    favorited_ids: set | None = None,
    ts_payload: dict | None = None,
) -> None:
    """Run Typesense alongside Postgres search; log mismatches (sampled)."""
    rate = getattr(settings, "SEARCH_SHADOW_SAMPLE_RATE", 0.05) or 0.0
    if rate <= 0 or random.random() > rate:
        return
    try:
        if ts_payload is None:
            ts_payload = run_public_class_search(request, favorited_ids=favorited_ids)
        raw_db = db_builder()
        db_payload = normalize_db_search_payload(raw_db)
        log_search_shadow_diff(ts_payload, db_payload, dict(request.query_params))
    except Exception as e:
        logger.debug("search_shadow compare skipped: %s", e)
