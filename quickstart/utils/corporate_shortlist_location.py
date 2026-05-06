"""Friendly location labels for corporate shortlist (avoid raw street lines in UI)."""


def _venue_area_label(name: str, city: str, state: str) -> str:
    """
    Combine venue name with city, state — drop the venue when it only repeats
    the city (e.g. avoid "Aurora · Aurora, Ontario").
    """
    name = (name or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    area = ", ".join(p for p in [city, state] if p)

    if not name:
        return area
    if not area:
        return name

    n_low = name.lower()
    if city and n_low == city.lower():
        return area

    first_seg = area.split(",")[0].strip().lower()
    if n_low == first_seg:
        return area

    return f"{name} · {area}"


def maps_search_query_for_option(option) -> str:
    """
    Best string for opening Maps — prefer full address when available for accuracy.
    """
    raw = (getattr(option, "location_text", None) or "").strip()
    if raw:
        return raw
    sc = getattr(option, "source_class", None)
    if sc is None:
        return ""
    loc_ref = getattr(sc, "location_ref", None)
    if loc_ref is not None:
        parts = [
            (getattr(loc_ref, "address", None) or "").strip(),
            (getattr(loc_ref, "city", None) or "").strip(),
            (getattr(loc_ref, "state", None) or "").strip(),
        ]
        line = ", ".join(p for p in parts if p)
        if line:
            return line
    city = (getattr(sc, "city", None) or "").strip()
    state = (getattr(sc, "state", None) or "").strip()
    return ", ".join(p for p in [city, state] if p)


def location_label_for_option(option) -> str:
    """
    Short label for comparison tables: city/region, venue · area, or trimmed free text.
    Does not replace precise location_text / maps query.
    """
    sc = getattr(option, "source_class", None)
    if sc is not None:
        loc_ref = getattr(sc, "location_ref", None)
        if loc_ref is not None:
            name = (getattr(loc_ref, "name", None) or "").strip()
            city = (getattr(loc_ref, "city", None) or "").strip()
            state = (getattr(loc_ref, "state", None) or "").strip()
            label = _venue_area_label(name, city, state)
            if label:
                return label
        city = (getattr(sc, "city", None) or "").strip()
        state = (getattr(sc, "state", None) or "").strip()
        parts = [p for p in [city, state] if p]
        if parts:
            return ", ".join(parts)

    raw = (getattr(option, "location_text", None) or "").strip()
    if not raw:
        return ""

    segments = [s.strip() for s in raw.split(",") if s.strip()]
    n = len(segments)
    if n >= 3:
        return ", ".join(segments[-2:])
    if n == 2:
        a, b = segments[0], segments[1]
        if len(b) <= 3 and b.isalpha():
            return f"{a}, {b}"
        return b
    return raw
