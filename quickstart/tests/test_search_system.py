"""
Tests for Typesense-backed public class search, shared filters, and DB fallback.

Run: pytest quickstart/tests/test_search_system.py -v
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from quickstart.services.search_engine_service import (
    run_public_class_search,
    _facet_or,
    _hydrate_card_row_image_medium_urls,
)
from quickstart.services.search_filter_params import (
    BOOKING_TYPE_FULL_COURSE,
    BOOKING_TYPE_SINGLE_SESSION,
    normalize_booking_type_query,
)
from quickstart.services.search_geo_params import (
    toronto_gta_typesense_location_clause,
    typesense_geo_radius_clause,
)
from quickstart.services.boundary_geometry_service import format_typesense_polygon_filter
from quickstart.tests.factories import (
    BusinessFactory,
    ClassMainFactory,
    ClassOptionFactory,
    ScheduleFactory,
    ScheduleInstanceFactory,
)

API = "/api"

_drf_factory = APIRequestFactory()


def _drf_request(path: str, query=None):
    """DRF Request with `.query_params` / `.GET` like the view passes to search."""
    if query is None:
        django_req = _drf_factory.get(path)
    elif isinstance(query, str):
        sep = "&" if "?" in path else "?"
        django_req = _drf_factory.get(f"{path}{sep}{query}")
    else:
        django_req = _drf_factory.get(path, query)
    return Request(django_req)


@pytest.fixture(autouse=True)
def _search_tests_disable_background_io(monkeypatch):
    """Prevent Celery broker / Redis hangs when factories trigger Typesense signals."""
    from django.core.cache.backends.locmem import LocMemCache

    loc = LocMemCache("pytest-search-system", {})
    monkeypatch.setattr("django.core.cache.cache", loc)
    monkeypatch.setattr("quickstart.services.search_engine_service.cache", loc)
    monkeypatch.setattr(
        "quickstart.tasks.search_index_tasks.reindex_class_task.delay",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "quickstart.tasks.search_index_tasks.rebuild_boundary_buffer_for_id_task.delay",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "quickstart.tasks.search_index_tasks.bootstrap_typesense_search_index_task.apply_async",
        lambda *a, **k: None,
    )


def _make_ts_hit(class_id: int, slug: str = "c-slug") -> dict:
    card = {
        "classId": class_id,
        "slug": slug,
        "images": [{"image_key": None, "medium_url": None, "isCover": True}],
    }
    return {
        "document": {
            "id": str(class_id),
            "card_json": json.dumps(card),
        }
    }


def _capture_search(mock_ts):
    """First positional arg to _typesense_search_collection is (client, coll, search_params)."""
    call = mock_ts.call_args
    assert call is not None
    _client, coll, params = call[0]
    return coll, params


class TestTorontoGtaGeoParams:
    def test_typesense_location_clause_uses_default_radius(self):
        clause = toronto_gta_typesense_location_clause(None)
        assert clause.startswith("location:(")
        assert "100.0 km" in clause

    def test_typesense_location_clause_honors_custom_radius(self):
        clause = toronto_gta_typesense_location_clause("50")
        assert "50.0 km" in clause

    def test_typesense_geo_radius_clause_rejects_invalid_coords(self):
        assert typesense_geo_radius_clause(float("nan"), -79.0, 100) is None
        assert typesense_geo_radius_clause(43.65, float("inf"), 100) is None
        assert typesense_geo_radius_clause(120.0, -79.0, 100) is None

    def test_facet_or_ignores_empty_values(self):
        assert _facet_or("collection_slugs", ["", "  ", "wellness"]) == (
            "(collection_slugs:=`wellness`)"
        )
        assert _facet_or("collection_slugs", ["", ""]) is None

    def test_polygon_filter_requires_three_valid_points(self):
        assert format_typesense_polygon_filter([(43.65, -79.38), (43.66, -79.39)]) is None
        poly = format_typesense_polygon_filter(
            [(43.65, -79.38), (43.66, -79.39), (43.67, -79.40)]
        )
        assert poly is not None
        assert poly.startswith("(")

    def test_invalid_lat_lng_omits_location_filter(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "not-a-number",
                "lng": "-79.3832",
                "location_search": "123 Fake Street",
            },
        )
        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)
        _, params = _capture_search(ts_search)
        assert "location:(" not in params.get("filter_by", "")

    def test_empty_collection_slug_omits_collection_filter(self):
        request = _drf_request(
            "/api/classes/search/",
            "lat=43.6532&lng=-79.3832&location_search=Toronto,+ON&collection=&collection=wellness",
        )
        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)
        _, params = _capture_search(ts_search)
        fb = params.get("filter_by", "")
        assert "collection_slugs:" in fb
        assert "wellness" in fb
        assert "collection_slugs:=``" not in fb


class TestNormalizeBookingTypeQuery:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, None),
            ("", None),
            ("  ", None),
            ("class", None),
            ("ALL", None),
            ("any", None),
            ("single_session", BOOKING_TYPE_SINGLE_SESSION),
            ("Single Session", BOOKING_TYPE_SINGLE_SESSION),
            ("single session", BOOKING_TYPE_SINGLE_SESSION),
            ("single", BOOKING_TYPE_SINGLE_SESSION),
            ("full_course", BOOKING_TYPE_FULL_COURSE),
            ("Full Course", BOOKING_TYPE_FULL_COURSE),
            ("course", BOOKING_TYPE_FULL_COURSE),
        ],
    )
    def test_aliases(self, raw, expected):
        assert normalize_booking_type_query(raw) == expected

    def test_unknown_returns_none(self):
        assert normalize_booking_type_query("not-a-real-type") is None


class TestHydrateCardImages:
    def test_fills_medium_url_from_image_key_when_cloudfront_configured(self, settings):
        settings.CLOUDFRONT_DOMAIN = "https://cdn.example.com"
        row = {
            "classId": 1,
            "images": [
                {
                    "image_key": "originals/foo/bar.webp",
                    "medium_url": None,
                    "isCover": True,
                }
            ],
        }
        _hydrate_card_row_image_medium_urls(row)
        assert row["images"][0]["medium_url"]
        assert "public/medium/foo/bar.webp" in row["images"][0]["medium_url"]

    def test_no_op_when_medium_already_set(self, settings):
        settings.CLOUDFRONT_DOMAIN = "https://cdn.example.com"
        existing = "https://cdn.example.com/public/medium/x.webp"
        row = {
            "classId": 1,
            "images": [
                {"image_key": "originals/foo/bar.webp", "medium_url": existing}
            ],
        }
        _hydrate_card_row_image_medium_urls(row)
        assert row["images"][0]["medium_url"] == existing


@pytest.mark.django_db
class TestRunPublicClassSearchMockedTypesense:
    @pytest.fixture(autouse=True)
    def _search_settings(self, settings):
        settings.SEARCH_RESULTS_CACHE_SECONDS = 0
        settings.TYPESENSE_COLLECTION_ALIAS = "classes_live"

    @pytest.fixture(autouse=True)
    def _mock_client(self):
        with patch(
            "quickstart.services.search_engine_service.get_typesense_client",
            return_value=MagicMock(),
        ):
            with patch(
                "quickstart.services.search_engine_service._physical_collection_name",
                return_value="physical_coll",
            ):
                with patch(
                    "quickstart.services.search_engine_service.ensure_physical_collection",
                ):
                    yield

    def test_count_only_uses_single_hit_and_excludes_card_json(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "count_only": "1",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={
                "hits": [{"document": {"id": "99"}}],
                "found": 42,
            },
        ) as ts_search:
            payload = run_public_class_search(request)

        assert payload["count"] == 42
        assert payload["results"] == []
        _, params = _capture_search(ts_search)
        assert params.get("exclude_fields") == "card_json"
        assert params["per_page"] == 1
        assert params["page"] == 1

    def test_class_type_adds_booking_types_filter(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "class_type": "full_course",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [_make_ts_hit(1)], "found": 1},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        fb = params.get("filter_by", "")
        assert "booking_types:" in fb
        assert "Full Course" in fb

    def test_price_min_and_price_max_in_filter_by(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "price_min": "10",
                "price_max": "200",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [_make_ts_hit(2)], "found": 1},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        fb = params["filter_by"].replace(" ", "")
        assert "min_price:>=10.0" in fb or "min_price:>=10" in fb
        assert "price_unknown:=true" in fb or "min_price:<=" in fb

    def test_participants_filter_max_capacity(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "participants": "4",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        assert "max_capacity:>=4" in params["filter_by"]

    def test_toronto_gta_uses_radius_not_city_polygon(self):
        """Toronto preset must match Postgres: 100km radius, not Toronto boundary polygon."""
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "participants": "1",
                "page_size": "24",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        fb = params["filter_by"]
        assert "100.0 km" in fb
        assert "43.7000000,-79.4000000" not in fb

    def test_multiple_collections_or_semantics_in_slugs(self):
        request = _drf_request(
            "/api/classes/search/",
            "lat=43.6532&lng=-79.3832&location_search=Toronto,+ON&collection=trending&collection=date-night",
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        fb = params["filter_by"]
        assert "collection_slugs:" in fb
        assert "trending" in fb and "date-night" in fb

    def test_time_preference_only_uses_time_buckets_not_synthetic_calendar(self):
        request = _drf_request(
            "/api/classes/search/",
            {
                "lat": "43.6532",
                "lng": "-79.3832",
                "location_search": "Toronto, ON",
                "time_preference": "Morning (6am-12pm)",
            },
        )

        with patch(
            "quickstart.services.search_engine_service._typesense_search_collection",
            return_value={"hits": [], "found": 0},
        ) as ts_search:
            run_public_class_search(request)

        _, params = _capture_search(ts_search)
        fb = params["filter_by"]
        assert "time_buckets:" in fb
        assert "Morning (6am-12pm)" in fb
        today_compact = int(timezone.now().date().strftime("%Y%m%d"))
        assert f"min_available_date:>={today_compact}" in fb
        assert fb.count("available_dates:=`") < 50


@pytest.mark.django_db
class TestPublicClassSearchViewEnginePaths:
    """HTTP integration with Typesense path mocked."""

    @pytest.fixture(autouse=True)
    def _search_settings(self, settings):
        settings.SEARCH_RESULTS_CACHE_SECONDS = 0

    def test_typesense_unavailable_returns_503(self, api_client):
        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=False,
        ):
            response = api_client.get(
                f"{API}/classes/search/",
                {"lat": "43.6532", "lng": "-79.3832", "location_search": "Toronto, ON"},
            )
        assert response.status_code == 503
        assert "error" in response.json()

    def test_typesense_exception_returns_503(self, api_client):
        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            with patch(
                "quickstart.services.search_engine_service.run_public_class_search",
                side_effect=RuntimeError("TS boom"),
            ):
                response = api_client.get(
                    f"{API}/classes/search/",
                    {
                        "lat": "43.6532",
                        "lng": "-79.3832",
                        "location_search": "Toronto, ON",
                    },
                )
        assert response.status_code == 503

    def test_count_only_returns_count_only_json(self, api_client):
        fake = {"count": 7, "next": None, "previous": None, "results": []}
        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            with patch(
                "quickstart.services.search_engine_service.run_public_class_search",
                return_value=fake,
            ):
                response = api_client.get(
                    f"{API}/classes/search/",
                    {
                        "lat": "43.6532",
                        "lng": "-79.3832",
                        "location_search": "Toronto, ON",
                        "count_only": "1",
                    },
                )
        assert response.status_code == 200
        body = response.json()
        assert body == {"count": 7}


@pytest.mark.django_db
class TestPublicClassSearchForceDb:
    """force_db_search uses Postgres path while Typesense stays available."""

    def test_force_db_search_skips_typesense(self, api_client):
        biz = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            slug="sf-db-biz",
        )
        cls = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="sf-db-class",
            city="Toronto",
            state="ON",
            coordinates="43.6532,-79.3832",
        )
        cls.point = Point(-79.3832, 43.6532, srid=4326)
        cls.save(update_fields=["point"])
        option = ClassOptionFactory(classId=cls, booking_type="Single Session")
        schedule = ScheduleFactory(option=option, price=Decimal("40.00"))
        ScheduleInstanceFactory(schedule=schedule, status="scheduled")

        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            with patch(
                "quickstart.services.search_engine_service.run_public_class_search"
            ) as mock_ts:
                response = api_client.get(
                    f"{API}/classes/search/",
                    {
                        "lat": "43.6532",
                        "lng": "-79.3832",
                        "location_search": "Toronto, ON",
                        "force_db_search": "1",
                        "page_size": "24",
                    },
                )

        mock_ts.assert_not_called()
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        ids = {r.get("classId") for r in data.get("results") or []}
        assert cls.classId in ids

    def test_db_class_type_filters_booking_type(self, api_client):
        biz = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            slug="sf-type-biz",
        )
        cls_a = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="sf-type-a",
            coordinates="43.6532,-79.3832",
        )
        cls_a.point = Point(-79.3832, 43.6532, srid=4326)
        cls_a.save(update_fields=["point"])
        opt_single = ClassOptionFactory(
            classId=cls_a,
            booking_type="Single Session",
        )
        sch_a = ScheduleFactory(option=opt_single, price=Decimal("30.00"))
        ScheduleInstanceFactory(schedule=sch_a, status="scheduled")

        cls_b = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="sf-type-b",
            coordinates="43.6532,-79.3832",
        )
        cls_b.point = Point(-79.3832, 43.6532, srid=4326)
        cls_b.save(update_fields=["point"])
        opt_course = ClassOptionFactory(
            classId=cls_b,
            booking_type="Full Course",
        )
        course_start = date.today() + timedelta(days=7)
        sch_b = ScheduleFactory(
            option=opt_course,
            price=Decimal("80.00"),
            date=None,
            start_date=course_start,
            end_date=course_start + timedelta(weeks=8),
            day="Sat",
        )
        ScheduleInstanceFactory(
            schedule=sch_b, date=course_start, status="scheduled"
        )

        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            resp = api_client.get(
                f"{API}/classes/search/",
                {
                    "lat": "43.6532",
                    "lng": "-79.3832",
                    "location_search": "Toronto, ON",
                    "class_type": "full_course",
                    "force_db_search": "1",
                    "page_size": "50",
                },
            )

        assert resp.status_code == 200
        ids = {r.get("classId") for r in resp.json().get("results") or []}
        assert cls_b.classId in ids
        assert cls_a.classId not in ids

    def test_db_price_min_excludes_cheaper_floor(self, api_client):
        biz = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            slug="sf-price-biz",
        )
        cheap = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="sf-cheap",
            coordinates="43.6532,-79.3832",
        )
        cheap.point = Point(-79.3832, 43.6532, srid=4326)
        cheap.save(update_fields=["point"])
        o1 = ClassOptionFactory(classId=cheap, booking_type="Single Session")
        s1 = ScheduleFactory(option=o1, price=Decimal("20.00"))
        ScheduleInstanceFactory(schedule=s1, status="scheduled")

        pricey = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="sf-pricey",
            coordinates="43.6532,-79.3832",
        )
        pricey.point = Point(-79.3832, 43.6532, srid=4326)
        pricey.save(update_fields=["point"])
        o2 = ClassOptionFactory(classId=pricey, booking_type="Single Session")
        s2 = ScheduleFactory(option=o2, price=Decimal("90.00"))
        ScheduleInstanceFactory(schedule=s2, status="scheduled")

        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            resp = api_client.get(
                f"{API}/classes/search/",
                {
                    "lat": "43.6532",
                    "lng": "-79.3832",
                    "location_search": "Toronto, ON",
                    "price_min": "50",
                    "force_db_search": "1",
                    "page_size": "50",
                },
            )

        assert resp.status_code == 200
        ids = {r.get("classId") for r in resp.json().get("results") or []}
        assert pricey.classId in ids
        assert cheap.classId not in ids


@pytest.mark.django_db
class TestPublicClassSearchTypesenseResponseShape:
    """Default search path uses Typesense (mocked here for stable CI)."""

    @pytest.fixture(autouse=True)
    def _cache_off(self, settings):
        settings.SEARCH_RESULTS_CACHE_SECONDS = 0

    def test_search_returns_paginated_results(self, api_client):
        biz = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            slug="ts-shape-biz",
        )
        cls = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="ts-shape-class",
            coordinates="43.6532,-79.3832",
        )
        cls.point = Point(-79.3832, 43.6532, srid=4326)
        cls.save(update_fields=["point"])
        opt = ClassOptionFactory(classId=cls)
        sch = ScheduleFactory(option=opt)
        ScheduleInstanceFactory(schedule=sch, status="scheduled")

        fake = {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [{"classId": cls.classId, "slug": "x"}],
        }
        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            with patch(
                "quickstart.services.search_engine_service.run_public_class_search",
                return_value=fake,
            ):
                response = api_client.get(
                    f"{API}/classes/search/",
                    {
                        "lat": "43.6532",
                        "lng": "-79.3832",
                        "location_search": "Toronto, ON",
                        "page_size": "24",
                    },
                )
        assert response.status_code == 200
        body = response.json()
        assert "count" in body and "results" in body
        assert isinstance(body["count"], int)
        assert body["results"]

    def test_count_only_returns_count_key_only(self, api_client):
        biz = BusinessFactory(
            isActive=True,
            verificationStatus="verified",
            slug="ts-count-biz",
        )
        cls = ClassMainFactory(
            businessId=biz,
            status="active",
            slug="ts-count-class",
            coordinates="43.6532,-79.3832",
        )
        cls.point = Point(-79.3832, 43.6532, srid=4326)
        cls.save(update_fields=["point"])
        opt = ClassOptionFactory(classId=cls)
        sch = ScheduleFactory(option=opt)
        ScheduleInstanceFactory(schedule=sch, status="scheduled")

        fake = {"count": 3, "next": None, "previous": None, "results": []}
        with patch(
            "quickstart.services.typesense_client.typesense_available",
            return_value=True,
        ):
            with patch(
                "quickstart.services.search_engine_service.run_public_class_search",
                return_value=fake,
            ):
                response = api_client.get(
                    f"{API}/classes/search/",
                    {
                        "lat": "43.6532",
                        "lng": "-79.3832",
                        "location_search": "Toronto, ON",
                        "count_only": "1",
                    },
                )
        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"count"}
        assert isinstance(data["count"], int)
        assert data["count"] == 3
