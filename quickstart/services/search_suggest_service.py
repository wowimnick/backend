"""
Keyword search helpers for `/api/search/suggest/` — classes mirror PublicClassViewSet semantics.
"""

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from quickstart.models import ClassCollection, ScheduleInstance


def base_public_classes_queryset():
    """Same filters and annotations as public list/detail (verified business, active class)."""
    # Local import avoids circular import: public_class_views imports this module.
    from quickstart.views.public.public_class_views import PublicClassViewSet

    view = PublicClassViewSet()
    view.action = "list"
    return view.get_queryset()


def match_collection_by_alias(keyword: str):
    """
    Best-effort match of free text to an active, searchable ClassCollection.
    Used when explore has ?keyword=... without ?collection= to narrow results.
    Returns ClassCollection or None.
    """
    kw = (keyword or "").strip().lower()
    if len(kw) < 2:
        return None

    base = ClassCollection.objects.filter(is_active=True, is_searchable=True)

    hit = base.filter(slug__iexact=kw).first()
    if hit:
        return hit

    hit = base.filter(name__iexact=kw).first()
    if hit:
        return hit

    hit = base.filter(search_aliases__contains=[kw]).first()
    if hit:
        return hit

    try:
        from django.contrib.postgres.search import TrigramSimilarity

        hit = (
            base.annotate(sim=TrigramSimilarity("name", kw))
            .filter(sim__gt=0.45)
            .order_by("-sim", "sort_order", "name")
            .first()
        )
        if hit:
            return hit
    except Exception:
        pass

    for coll in base.order_by("sort_order", "name"):
        for alias in coll.search_aliases or []:
            al = str(alias).strip().lower()
            if not al:
                continue
            if al == kw or (len(kw) >= 3 and (kw in al or al in kw)):
                return coll

    return None


def queryset_classes_with_keyword(keyword: str):
    """Full-text filter on ClassesMain.search_vector."""
    search_query = SearchQuery(keyword, search_type="websearch", config="english")
    return (
        base_public_classes_queryset()
        .annotate(rank=SearchRank(F("search_vector"), search_query))
        .filter(search_vector=search_query)
    )


def queryset_classes_keyword_ordered(keyword: str, user_location_point=None):
    """
    Classes matching keyword, only those with future scheduled instances.
    Optionally sort by distance after rank.
    """
    has_future_instances = ScheduleInstance.objects.filter(
        schedule__option__classId=OuterRef("pk"),
        date__gte=timezone.now().date(),
        status="scheduled",
    )
    qs = queryset_classes_with_keyword(keyword).filter(Exists(has_future_instances))
    order_fields = ["-rank", "-createdAt"]
    if user_location_point is not None:
        qs = qs.annotate(distance=Distance("point", user_location_point))
        order_fields = ["-rank", "distance"]
    return qs.order_by(*order_fields)


def suggest_class_instances(keyword: str, limit: int, lat_str=None, lng_str=None):
    """
    Returns a list of ClassesMain instances (images prefetched via base queryset).
    For queries of length <= 2, returns [] (collections-only short-prefix UX).
    """
    kw = (keyword or "").strip()
    if len(kw) <= 2:
        return []

    user_location_point = None
    if lat_str and lng_str:
        try:
            user_location_point = Point(float(lng_str), float(lat_str), srid=4326)
        except (ValueError, TypeError):
            user_location_point = None

    qs = queryset_classes_keyword_ordered(kw, user_location_point)
    # Images (and options) already prefetched on base_public_classes_queryset();
    # do not add Prefetch("images") again — Django forbids duplicate lookups.
    return list(qs[:limit])
