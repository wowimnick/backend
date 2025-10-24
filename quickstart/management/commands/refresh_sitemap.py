from django.core.management.base import BaseCommand
from django.core.cache import cache


class Command(BaseCommand):
    help = "Refresh sitemap by clearing cache"

    def add_arguments(self, parser):
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify sitemap generation after clearing cache",
        )

    def handle(self, *args, **options):
        """Clear cache to force sitemap regeneration."""

        self.stdout.write("Clearing cache...")

        try:
            # Simple and reliable: clear entire cache
            cache.clear()

            self.stdout.write(self.style.SUCCESS("✅ Cache cleared successfully!"))
            self.stdout.write(
                self.style.SUCCESS("✅ Sitemap will be regenerated on next request.")
            )

            if options["verify"]:
                self.stdout.write("\nVerifying sitemap generation...")
                self._verify_sitemap()

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Error clearing cache: {str(e)}"))
            raise

    def _verify_sitemap(self):
        """Test sitemap generation."""
        try:
            from quickstart.sitemaps import (
                ExplorePagesSitemap,
                StaticViewSitemap,
                ClassSitemap,
                BusinessSitemap,
            )

            sitemaps = {
                "Static": StaticViewSitemap(),
                "Classes": ClassSitemap(),
                "Businesses": BusinessSitemap(),
                "Explore": ExplorePagesSitemap(),
            }

            total = 0
            for name, sitemap in sitemaps.items():
                try:
                    count = len(list(sitemap.items()))
                    total += count
                    self.stdout.write(f"  • {name}: {count:,} URLs")
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"  • {name}: Failed - {str(e)}")
                    )

            self.stdout.write(
                self.style.SUCCESS(f"\n✅ Total: {total:,} URLs generated")
            )

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Verification failed: {str(e)}"))
