"""Per-endpoint perf budgets for GET smoke + query caps.

Targets approximate what we want in production for uncached anonymous reads on
typical hardware: catch N+1 and pathological latency without matching empty-CI
DB minima exactly. Ratchet `BUDGETS` down as views stay optimized.
"""

# Routes not listed below use these ceilings (single GET, one page of data).
DEFAULT_MAX_QUERIES = 120
DEFAULT_MAX_MS = 3000

# Tighter budgets for hot or sensitive routes (name = Django URL name).
BUDGETS = {
    "health-check": {"max_queries": 8, "max_ms": 400},
    "api-root-health": {"max_queries": 8, "max_ms": 400},
    # Browsable API can be slow on first hit (schema/render); not DB-bound (often 0 queries).
    "api-root": {"max_queries": 24, "max_ms": 2500},
    "csrf_cookie": {"max_queries": 6, "max_ms": 400},
    "public-class-homepage-content": {"max_queries": 60, "max_ms": 2200},
    "public-class-search": {"max_queries": 80, "max_ms": 2500},
    "public-class-list": {"max_queries": 70, "max_ms": 2500},
    "public-business-list": {"max_queries": 85, "max_ms": 2800},
    "public-business-list-reviews": {"max_queries": 90, "max_ms": 2800},
}


def budget_for(name: str):
    b = BUDGETS.get(name) or {}
    return {
        "max_queries": b.get("max_queries", DEFAULT_MAX_QUERIES),
        "max_ms": b.get("max_ms", DEFAULT_MAX_MS),
    }
