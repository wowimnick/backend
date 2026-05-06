"""
GET perf smoke: one request per simple (non-regex) route, query cap + wall-clock budget.

Inventory from `python manage.py list_endpoints`.

Live timing + query counts (every endpoint):

    pytest quickstart/tests/perf/test_endpoint_perf.py -o log_cli=true --log-cli-level=INFO
"""

import json
import logging
import time
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from quickstart.tests.perf.budgets import budget_for

INVENTORY = Path(__file__).resolve().parent / "endpoint_inventory.json"

logger = logging.getLogger(__name__)

PERF_EXCLUDE_PREFIXES = (
    "/admin/",
    "/accounts/",
    "/impersonate/",
)


def _load_cases():
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))
    seen_paths = set()
    out = []
    for row in rows:
        if not row.get("perf_get"):
            continue
        path = row["path"]
        if any(path.startswith(p) for p in PERF_EXCLUDE_PREFIXES):
            continue
        if path in seen_paths:
            continue
        seen_paths.add(path)
        out.append(row)
    return out


def _case_id(row):
    name = row.get("name") or "noname"
    p = row.get("path", "").strip("/").replace("/", "_") or "root"
    return f"{name}-{p}"


@pytest.mark.django_db
@pytest.mark.parametrize("row", _load_cases(), ids=_case_id)
def test_get_endpoint_query_and_latency_budget(api_client, row):
    path = row["path"]
    if path == "/":
        api_path = "/api/"
    else:
        api_path = "/api/" + path.lstrip("/")
        if not api_path.endswith("/"):
            api_path += "/"
    api_path = api_path.replace("//", "/")

    name = row.get("name") or ""
    b = budget_for(name)

    with CaptureQueriesContext(connection) as query_ctx:
        t0 = time.perf_counter()
        response = api_client.get(api_path)
        elapsed_ms = (time.perf_counter() - t0) * 1000

    num_queries = len(query_ctx)
    logger.info(
        "perf_get name=%r path=%s status=%s queries=%s/%s elapsed_ms=%.1f budget_ms=%s",
        name,
        api_path,
        response.status_code,
        num_queries,
        b["max_queries"],
        elapsed_ms,
        b["max_ms"],
    )

    assert num_queries <= b["max_queries"], (
        f"{api_path} name={name!r} executed {num_queries} queries "
        f"(budget {b['max_queries']})"
    )

    assert response.status_code in (
        200,
        204,
        301,
        302,
        400,
        401,
        403,
        404,
        405,
    ), (
        f"{api_path} name={name!r} -> {response.status_code} "
        f"{getattr(response, 'content', b'')[:300]!r}"
    )
    assert elapsed_ms < b["max_ms"], (
        f"{api_path} took {elapsed_ms:.1f}ms (budget {b['max_ms']}ms)"
    )
