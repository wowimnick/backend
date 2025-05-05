from django.contrib import admin
from django.urls import path, include
from django.contrib.sitemaps.views import sitemap 
from quickstart.sitemaps import StaticViewSitemap, ClassSitemap 

sitemaps = {
    'static': StaticViewSitemap,
    'classes': ClassSitemap,
    # Add other sitemaps like BusinessProfileSitemap if you implement it
}

urlpatterns = [
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),
    path('admin/', admin.site.urls),
    path('api/', include('quickstart.urls')),
]
