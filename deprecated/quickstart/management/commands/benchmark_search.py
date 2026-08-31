"""Compare in-process latency: Typesense vs Postgres public class search."""

from __future__ import annotations

import statistics
import time
import traceback
from typing import Any

from django.core.management.base import BaseCommand
from django.test import override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

# Slugs that mean "no collection filter" in the app UI — useless for benchmarking.
_RESERVED_BENCHMARK_COLLECTION_SLUGS = frozenset(
    s.lower() for s in ("all", "any", "class")
)


def _pick_benchmark_collection_slug() -> str | None:
    from quickstart.models import ClassCollection

    for slug in ClassCollection.objects.order_by("sort_order", "id").values_list(
        "slug", flat=True,
    ):
        if slug and str(slug).lower() not in _RESERVED_BENCHMARK_COLLECTION_SLUGS:
            return str(slug)
    return None


SCENARIOS: dict[str, str] = {
    "broad_geo_relevance": (
        "lat=43.6532&lng=-79.3832&location_search=Toronto,+ON&radius=50&page_size=24"
    ),
    "keyword_fts": "keyword=yoga&page_size=24",
    "collection_filter": "collection={collection}&class_type=single_session&page_size=24",
    "distance_sort": "lat=43.6532&lng=-79.3832&radius=25&sort_by=distance&page_size=24",
    "count_only_geo": "lat=43.6532&lng=-79.3832&radius=50&count_only=1",
}


def _build_request(query_string: str) -> Request:
    django_request = APIRequestFactory().get(
        f"/api/classes/search/?{query_string}",
    )
    return Request(django_request)


def _extract_count_from_db_response(response: Any) -> int | None:
    data = getattr(response, "data", None)
    if isinstance(data, dict) and "count" in data:
        return data["count"]
    if isinstance(data, list):
        return len(data)
    return None


def _try_mean(samples: list[float]) -> float | None:
    return statistics.mean(samples) if samples else None


def _try_median(samples: list[float]) -> float | None:
    return statistics.median(samples) if samples else None


def _try_min(samples: list[float]) -> float | None:
    return min(samples) if samples else None


def _counts_diverge(ts_count: int | None, pg_count: int | None, rel: float = 0.05) -> bool:
    if ts_count is None or pg_count is None:
        return False
    if ts_count == pg_count:
        return False
    denom = max(abs(ts_count), abs(pg_count), 1)
    return abs(ts_count - pg_count) / denom > rel


def _run_one_engine(
    *,
    engine: str,
    qs: str,
    iterations: int,
    warmup: int,
    viewset: Any,
) -> tuple[list[float], int | None, str | None]:
    """
    Returns (timing_ms_samples, last_success_count, first_error_summary_or_none).
    """
    from quickstart.services.search_engine_service import run_public_class_search

    samples: list[float] = []
    last_count: int | None = None
    first_error: str | None = None

    def _warmup_and_measure() -> None:
        nonlocal last_count, first_error
        total = warmup + iterations
        for i in range(total):
            is_warmup = i < warmup
            req = _build_request(qs)
            t0 = time.perf_counter()
            try:
                if engine == "typesense":
                    payload = run_public_class_search(req, favorited_ids=set())
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if not is_warmup:
                        samples.append(elapsed_ms)
                        last_count = payload.get("count")
                else:
                    viewset.request = req
                    viewset.format_kwarg = None
                    resp = viewset._public_class_search_database(req)
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if not is_warmup:
                        samples.append(elapsed_ms)
                        last_count = _extract_count_from_db_response(resp)
            except Exception:
                if not is_warmup and first_error is None:
                    first_error = traceback.format_exc().strip().split("\n")[-1]

    _warmup_and_measure()
    return samples, last_count, first_error


class Command(BaseCommand):
    help = (
        "Benchmark public class search: Typesense (run_public_class_search) vs "
        "Postgres (_public_class_search_database). Uses live DB and Typesense."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--iterations",
            type=int,
            default=5,
            help="Timed runs per scenario per engine (default: 5)",
        )
        parser.add_argument(
            "--warmup",
            type=int,
            default=1,
            help="Throwaway runs before timing each scenario/engine (default: 1)",
        )
        parser.add_argument(
            "--query",
            type=str,
            default="",
            help="Run only this scenario name (e.g. broad_geo_relevance)",
        )
        parser.add_argument(
            "--engine",
            type=str,
            choices=("both", "typesense", "postgres"),
            default="both",
            help="Which engine to time (default: both)",
        )

    def handle(self, *args, **options):
        iterations = max(1, int(options["iterations"]))
        warmup = max(0, int(options["warmup"]))
        query_filter = (options["query"] or "").strip()
        engine_mode: str = options["engine"]

        import logging

        from quickstart.services.typesense_client import typesense_available
        from quickstart.views.public.public_class_views import PublicClassViewSet

        collection_slug = _pick_benchmark_collection_slug()

        bench_log = logging.getLogger("quickstart.views.public.public_class_views")
        prev_bench_level = bench_log.level
        try:
            bench_log.setLevel(logging.WARNING)

            scenarios_run: list[str] = []
            if query_filter:
                if query_filter not in SCENARIOS:
                    self.stderr.write(
                        self.style.ERROR(
                            f"Unknown scenario {query_filter!r}. "
                            f"Choices: {', '.join(SCENARIOS)}"
                        ),
                    )
                    return
                scenarios_run = [query_filter]
            else:
                scenarios_run = list(SCENARIOS.keys())

            typesense_ok = typesense_available()
            if engine_mode in ("both", "typesense") and not typesense_ok:
                self.stderr.write(
                    self.style.WARNING(
                        "Typesense is not configured (TYPESENSE_API_KEY / host). "
                        "Skipping Typesense timings.",
                    ),
                )
                if engine_mode == "typesense":
                    return

            viewset = PublicClassViewSet()
            # get_serializer_class / get_queryset gate on action; bare ViewSet() has no action.
            viewset.action = "list"
            speedup_ratios: list[float] = []

            with override_settings(SEARCH_RESULTS_CACHE_SECONDS=0):
                for scenario_name in scenarios_run:
                    template = SCENARIOS[scenario_name]
                    if "{collection}" in template:
                        if not collection_slug:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"Scenario {scenario_name}: no ClassCollection rows; skipped.",
                                ),
                            )
                            continue
                        qs = template.format(collection=collection_slug)
                    else:
                        qs = template

                    self.stdout.write(self.style.MIGRATE_HEADING(f"Scenario: {scenario_name}"))
                    self.stdout.write(f"  Query: ?{qs}")

                    ts_samples: list[float] = []
                    pg_samples: list[float] = []
                    ts_count: int | None = None
                    pg_count: int | None = None
                    ts_err: str | None = None
                    pg_err: str | None = None

                    if engine_mode in ("both", "typesense") and typesense_ok:
                        ts_samples, ts_count, ts_err = _run_one_engine(
                            engine="typesense",
                            qs=qs,
                            iterations=iterations,
                            warmup=warmup,
                            viewset=viewset,
                        )
                    if engine_mode in ("both", "postgres"):
                        pg_samples, pg_count, pg_err = _run_one_engine(
                            engine="postgres",
                            qs=qs,
                            iterations=iterations,
                            warmup=warmup,
                            viewset=viewset,
                        )

                    if engine_mode in ("both", "typesense") and typesense_ok:
                        if ts_samples:
                            self.stdout.write(
                                "  Typesense:  "
                                f"count={ts_count!s:>6}   "
                                f"min={_try_min(ts_samples):7.1f} ms   "
                                f"median={_try_median(ts_samples):7.1f} ms   "
                                f"mean={_try_mean(ts_samples):7.1f} ms"
                            )
                        elif ts_err:
                            self.stdout.write(f"  Typesense:  ERROR — {ts_err}")
                        else:
                            self.stdout.write("  Typesense:  no samples")

                    if engine_mode in ("both", "postgres"):
                        if pg_samples:
                            self.stdout.write(
                                "  Postgres:   "
                                f"count={pg_count!s:>6}   "
                                f"min={_try_min(pg_samples):7.1f} ms   "
                                f"median={_try_median(pg_samples):7.1f} ms   "
                                f"mean={_try_mean(pg_samples):7.1f} ms"
                            )
                        elif pg_err:
                            self.stdout.write(f"  Postgres:   ERROR — {pg_err}")
                        else:
                            self.stdout.write("  Postgres:   no samples")

                    if _counts_diverge(ts_count, pg_count):
                        self.stdout.write(
                            self.style.WARNING(
                                f"  Note: count mismatch Typesense={ts_count} vs Postgres={pg_count}",
                            ),
                        )

                    ts_med = _try_median(ts_samples) if ts_samples else None
                    pg_med = _try_median(pg_samples) if pg_samples else None
                    if (
                        engine_mode == "both"
                        and typesense_ok
                        and ts_med is not None
                        and pg_med is not None
                        and ts_med > 0
                    ):
                        ratio = pg_med / ts_med
                        speedup_ratios.append(ratio)
                        if ratio >= 1:
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"  Typesense is {ratio:.1f}x faster than Postgres (median).",
                                ),
                            )
                        else:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  Typesense is {1/ratio:.1f}x slower than Postgres (median).",
                                ),
                            )

                    self.stdout.write("")

            if engine_mode == "both" and typesense_ok and speedup_ratios:
                overall = statistics.median(speedup_ratios)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Overall median speedup (Typesense vs Postgres): {overall:.1f}x",
                    ),
                )
        finally:
            bench_log.setLevel(prev_bench_level)
