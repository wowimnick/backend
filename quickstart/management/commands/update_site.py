# Create this file: quickstart/management/commands/update_site.py

from django.core.management.base import BaseCommand
from django.contrib.sites.models import Site
from django.conf import settings


class Command(BaseCommand):
    help = "Update site information for proper email templates"

    def handle(self, *args, **options):
        try:
            site = Site.objects.get(pk=1)
            site.name = "ClassEasily"
            site.domain = settings.SITE_DOMAIN
            site.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully updated site: {site.name} - {site.domain}"
                )
            )
        except Site.DoesNotExist:
            site = Site.objects.create(
                pk=1, name="ClassEasily", domain=settings.SITE_DOMAIN
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully created site: {site.name} - {site.domain}"
                )
            )
