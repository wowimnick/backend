"""
Emit API endpoint inventory for perf tests and snapshot scripts.

Usage:
  python manage.py list_endpoints [--output quickstart/tests/perf/endpoint_inventory.json]
"""

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver

from quickstart import urls as quickstart_urls


def _callback_methods(callback):
    actions = getattr(callback, "actions", None)
    if actions:
        return sorted(m.upper() for m in actions)
    view_cls = getattr(callback, "view_class", None)
    if view_cls is not None:
        out = []
        for meth in ("get", "post", "put", "patch", "delete", "head", "options"):
            if not hasattr(view_cls, meth):
                continue
            fn = getattr(view_cls, meth)
            if getattr(fn, "__name__", "") == "http_method_not_allowed":
                continue
            out.append(meth.upper())
        return out or ["GET"]
    return ["GET"]


def _walk(patterns, prefix_parts, rows):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            part = str(pattern.pattern)
            _walk(pattern.url_patterns, prefix_parts + [part], rows)
        elif isinstance(pattern, URLPattern):
            raw_pat = str(pattern.pattern)
            part = raw_pat
            if part.startswith("^") or "(?P<" in part:
                # DRF router regex routes — need kwargs; not used for simple perf GET sweep.
                requires_args = True
                is_regex = True
            else:
                requires_args = "<" in part
                is_regex = False
            path = "/".join(prefix_parts + [part])
            path = "/" + path.strip("/")
            path = re.sub(r"/+", "/", path)
            if not path.endswith("/"):
                path = path + "/"
            if "FORMAT_SUFFIX" in raw_pat or "drf_format_suffix" in raw_pat.lower():
                continue
            name = pattern.name or ""
            callback = pattern.callback
            methods = _callback_methods(callback)
            row = {
                "name": name,
                "path": path,
                "methods": methods,
                "requires_args": requires_args,
                "perf_get": "GET" in methods and not requires_args and not is_regex,
            }
            rows.append(row)


def _dedupe_rows(rows):
    seen = set()
    out = []
    for row in rows:
        key = (row["path"], tuple(row["methods"]), row["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


class Command(BaseCommand):
    help = "Write endpoint inventory JSON (for perf + snapshot tooling)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="quickstart/tests/perf/endpoint_inventory.json",
            help="Path relative to project root (backend/)",
        )

    def handle(self, *args, **options):
        rel = options["output"]
        root = Path(__file__).resolve().parents[3]
        out_path = root / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        _walk(quickstart_urls.urlpatterns, [], rows)
        rows = _dedupe_rows(rows)
        # Stable order: path then name
        rows.sort(key=lambda r: (r["path"], r["name"]))
        out_path.write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(rows)} endpoints to {out_path}")
        )
