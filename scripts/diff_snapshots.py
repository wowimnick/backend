#!/usr/bin/env python3
"""
Compare live GET responses to golden snapshots from capture_endpoint_snapshots.py.

Usage:
  export SNAPSHOT_BASE_URL=https://api-sandbox.example.com
  python scripts/diff_snapshots.py
  # exit 1 if any golden file differs
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "quickstart" / "tests" / "perf" / "endpoint_inventory.json"
SNAP_DIR = ROOT / "quickstart" / "tests" / "perf" / "snapshots"

# Import normalizer by exec minimal duplicate to avoid package path issues
REVIEW_PLACEHOLDER = "<REVIEW_COUNT_NORMALIZED>"


def normalize(obj):
    if isinstance(obj, dict):
        out = {}
        for k in sorted(obj.keys()):
            if k in ("created_at", "updated_at", "expires_at", "sent_at", "scheduled_at"):
                continue
            if k == "review_count":
                out[k] = REVIEW_PLACEHOLDER
                continue
            out[k] = normalize(obj[k])
        return out
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    return obj


def fetch_json(url: str, auth: str):
    req = urllib.request.Request(url, method="GET")
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body.strip() else {}


def main() -> int:
    import hashlib
    import re

    base = os.environ.get("SNAPSHOT_BASE_URL", "").rstrip("/")
    if not base:
        print("Set SNAPSHOT_BASE_URL", file=sys.stderr)
        return 2

    auth = os.environ.get("SNAPSHOT_AUTH_HEADER", "").strip()

    def safe_name(name: str, path: str) -> str:
        raw = f"{name}-{path}" or path
        h = hashlib.sha256(raw.encode()).hexdigest()[:12]
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name or path)[:80]
        return f"{slug}_{h}.golden.json"

    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))
    failed = 0
    for row in rows:
        if not row.get("perf_get"):
            continue
        gn = safe_name(row.get("name", ""), row.get("path", ""))
        golden_path = SNAP_DIR / gn
        if not golden_path.is_file():
            continue
        path = row["path"]
        if path == "/":
            url = base + "/api/"
        else:
            url = base + "/api/" + path.lstrip("/")
        if "?" not in url:
            url = url.rstrip("/") + "/"
        try:
            live = normalize(fetch_json(url, auth))
        except Exception as e:
            print("FETCH FAIL", url, e)
            failed += 1
            continue
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        if golden != live:
            print("MISMATCH", url, golden_path)
            failed += 1

    if failed:
        print(f"{failed} snapshot(s) differ or failed", file=sys.stderr)
        return 1
    print("All compared snapshots match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
