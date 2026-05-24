"""Tests for class slug regeneration and retired-slug redirects."""

from __future__ import annotations

import pytest

from quickstart.models import ClassSlugRedirect
from quickstart.tests.factories import BusinessFactory, ClassMainFactory
from quickstart.utils.class_slug_utils import record_class_slug_redirect, resolve_class_by_slug


@pytest.mark.django_db
class TestClassSlugRedirect:
    def test_title_change_regenerates_slug(self):
        business = BusinessFactory(businessCity="Toronto")
        klass = ClassMainFactory(
            businessId=business,
            title="Original Pottery Class",
            status="active",
        )
        old_slug = klass.slug
        assert old_slug

        klass.title = "Candlelit Pottery Series"
        klass.save()
        klass.refresh_from_db()

        assert klass.slug != old_slug
        assert "candlelit" in klass.slug.lower()
        assert ClassSlugRedirect.objects.filter(slug=old_slug, class_ref=klass).exists()

    def test_resolve_class_by_slug_follows_redirect(self):
        business = BusinessFactory(businessCity="Toronto")
        klass = ClassMainFactory(businessId=business, title="Handbuilding Workshop")
        old_slug = klass.slug

        record_class_slug_redirect(old_slug, klass)
        klass.slug = "toronto-new-handbuilding-workshop"
        klass.save(update_fields=["slug"])

        from quickstart.models import ClassesMain

        qs = ClassesMain.objects.filter(pk=klass.pk)
        resolved = resolve_class_by_slug(qs, old_slug)
        assert resolved.pk == klass.pk
        assert resolved.slug == klass.slug

    def test_same_slugify_title_does_not_create_redirect(self):
        business = BusinessFactory(businessCity="Toronto")
        klass = ClassMainFactory(businessId=business, title="Pottery 101")
        old_slug = klass.slug

        klass.title = "pottery 101"
        klass.save()
        klass.refresh_from_db()

        assert klass.slug == old_slug
        assert not ClassSlugRedirect.objects.filter(slug=old_slug).exists()
