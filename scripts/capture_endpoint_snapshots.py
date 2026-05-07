#!/usr/bin/env python3
"""
Capture normalized JSON snapshots for GET endpoints (staging or any base URL).

Usage:
  export SNAPSHOT_BASE_URL=https://api-staging.example.com
  export SNAPSHOT_AUTH_HEADER="Bearer <token>"   # optional, for authenticated routes
  python scripts/capture_endpoint_snapshots.py

Writes quickstart/tests/perf/snapshots/<safe-name>.golden.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "quickstart" / "tests" / "perf" / "endpoint_inventory.json"
OUT_DIR = ROOT / "quickstart" / "tests" / "perf" / "snapshots"


def _normalize(obj):
    """Strip noisy fields / sort keys for stable diff."""
    if isinstance(obj, dict):
        out = {}
        for k in sorted(obj.keys()):
            if k in ("created_at", "updated_at", "expires_at", "sent_at", "scheduled_at"):
                continue
            if k == "review_count":
                out[k] = "<REVIEW_COUNT_NORMALIZED>"
                continue
            out[k] = _normalize(obj[k])
        return out
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    return obj


def _safe_name(name: str, path: str) -> str:
    raw = f"{name}-{path}" or path
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name or path)[:80]
    return f"{slug}_{h}"


def main():
    base = os.environ.get("SNAPSHOT_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("Set SNAPSHOT_BASE_URL (e.g. https://api-staging...)")

    auth = os.environ.get("SNAPSHOT_AUTH_HEADER", "").strip()

    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for row in rows:
        if not row.get("perf_get"):
            continue
        path = row["path"]
        if path == "/":
            url = base + "/api/"
        else:
            url = base + "/api/" + path.lstrip("/")
        if "?" not in url:
            url = url.rstrip("/") + "/"

        req = urllib.request.Request(url, method="GET")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(body) if body.strip() else {}
        except Exception as e:
            print(f"SKIP {url}: {e}")
            continue

        normalized = _normalize(data)
        fn = _safe_name(row.get("name", ""), row.get("path", "")) + ".golden.json"
        out_path = OUT_DIR / fn
        out_path.write_text(
            json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print("Wrote", out_path)


if __name__ == "__main__":
    main()
