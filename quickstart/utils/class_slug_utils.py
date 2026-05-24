"""Resolve public class rows by slug, including retired slug redirects."""

from __future__ import annotations

from quickstart.models import ClassSlugRedirect, ClassesMain


def resolve_class_by_slug(queryset, slug: str) -> ClassesMain:
    """
    Return the class matching ``slug`` on the queryset, or follow a ClassSlugRedirect.
    Raises ClassesMain.DoesNotExist when nothing matches.
    """
    slug = (slug or "").strip()
    if not slug:
        raise ClassesMain.DoesNotExist
    try:
        return queryset.get(slug=slug)
    except ClassesMain.DoesNotExist:
        redirect_row = (
            ClassSlugRedirect.objects.filter(slug=slug)
            .select_related("class_ref")
            .first()
        )
        if not redirect_row:
            raise
        obj = queryset.filter(pk=redirect_row.class_ref_id).first()
        if obj is None:
            raise
        return obj


def record_class_slug_redirect(old_slug: str | None, class_instance: ClassesMain) -> None:
    """Persist ``old_slug`` -> class when a rename changes the canonical slug."""
    old = (old_slug or "").strip()
    new = (getattr(class_instance, "slug", None) or "").strip()
    if not old or not new or old == new or not class_instance.pk:
        return
    ClassSlugRedirect.objects.get_or_create(
        slug=old,
        defaults={"class_ref_id": class_instance.pk},
    )
