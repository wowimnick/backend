#!/usr/bin/env python3
"""Measure max queries per GET endpoint; print JSON for pasting into budgets.py (optional)."""
import json
import os
import sys
from pathlib import Path

# Run from repo root: python quickstart/tests/perf/capture_baseline.py

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "CEBackend.settings")

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from quickstart.tests.perf import budgets as budgets_mod  # noqa: E402

INVENTORY = ROOT / "quickstart" / "tests" / "perf" / "endpoint_inventory.json"
PREFIX_SKIP = ("/admin/", "/accounts/", "/impersonate/")


def main():
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))
    client = Client()
    out = {}
    for row in rows:
        if not row.get("perf_get"):
            continue
        path = row["path"]
        if any(path.startswith(p) for p in PREFIX_SKIP):
            continue
        if path == "/":
            api_path = "/api/"
        else:
            api_path = "/api/" + path.lstrip("/")
            if not api_path.endswith("/"):
                api_path += "/"
        api_path = api_path.replace("//", "/")
        name = row.get("name") or ""
        try:
            from django.db import connection

            with CaptureQueriesContext(connection) as ctx:
                client.get(api_path)
            n = len(ctx.captured_queries)
        except Exception as e:
            print(name, api_path, "ERR", e, file=sys.stderr)
            continue
        b = budgets_mod.budget_for(name)
        out[name or api_path] = {
            "captured_queries": n,
            "suggested_max_queries": int(n * 1.1) + 2,
            "current_cap": b["max_queries"],
        }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
