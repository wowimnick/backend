from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from django.contrib.sitemaps.views import sitemap  # Import the sitemap view

# Import your sitemap classes from your app
from quickstart.sitemaps import StaticViewSitemap, ClassSitemap

# Define the sitemaps dictionary here, at the project level
sitemaps = {
    "static": StaticViewSitemap,
    "classes": ClassSitemap,
}

urlpatterns = [
    # 1. Admin Interface (at the root)
    path("classeasily-control-panel/", admin.site.urls),
    # 2. API Routes (all prefixed with 'api/')j
    # This will include all the URLs from your quickstart/urls.py file.
    path("api/", include("quickstart.urls")),
    # 3. Sitemap URL (at the root)
    # This is the corrected placement. It's now outside the 'api/' prefix.
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    # 4. React Frontend Routes (at the root)
    path("", TemplateView.as_view(template_name="index.html"), name="homepage"),
    path("explore", TemplateView.as_view(template_name="index.html"), name="explore"),
    path(
        "business",
        TemplateView.as_view(template_name="index.html"),
        name="business-welcome",
    ),
    path("careers", TemplateView.as_view(template_name="index.html"), name="careers"),
    path("about-us", TemplateView.as_view(template_name="index.html"), name="about-us"),
    path("giftcard", TemplateView.as_view(template_name="index.html"), name="giftcard"),
    path(
        "terms-of-service",
        TemplateView.as_view(template_name="index.html"),
        name="terms-of-service",
    ),
    path(
        "privacy-policy",
        TemplateView.as_view(template_name="index.html"),
        name="privacy-policy",
    ),
]
