"""Gallery URLs for corporate shortlist options linked to ClassesMain + ClassImage."""

from quickstart.models import CorporateShortlistOption


def ordered_class_images(class_obj):
    """Cover first, then stable order (aligned with homepage / card sorting)."""
    if not class_obj:
        return []
    qs = getattr(class_obj, "images", None)
    if qs is None:
        return []
    image_list = list(qs.all()) if hasattr(qs, "all") else list(qs)
    if not image_list:
        return []
    return sorted(
        image_list,
        key=lambda img: (
            not getattr(img, "isCover", False),
            getattr(img, "createdAt", None) or "",
        ),
    )


def large_urls_for_class(class_obj):
    """Public CloudFront `large` URLs for each class image, in display order."""
    from quickstart.serializers.public.public_class_serializers import (
        PublicClassImageSerializer,
    )

    ser = PublicClassImageSerializer()
    urls = []
    for img in ordered_class_images(class_obj):
        u = ser.get_large_url(img)
        if u:
            urls.append(u)
    return urls


def _dedupe_preserve_order(urls):
    seen = set()
    out = []
    for u in urls:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def effective_gallery_urls_for_option(option):
    """
    Manual `gallery_urls` JSON plus, for options tied to an existing class,
    every additional class photo after the first (cover is `cover_image_url`).

    Options created via "add from class" store `gallery_urls=[]`; photos live on ClassImage.
    """
    stored = [u for u in (option.gallery_urls or []) if u]
    if option.source_type != CorporateShortlistOption.SOURCE_EXISTING_CLASS:
        return _dedupe_preserve_order(stored)

    sc = getattr(option, "source_class", None)
    if sc is None:
        return _dedupe_preserve_order(stored)

    all_large = large_urls_for_class(sc)
    if len(all_large) <= 1:
        return _dedupe_preserve_order(stored)

    extras_from_class = all_large[1:]
    return _dedupe_preserve_order(extras_from_class + stored)
