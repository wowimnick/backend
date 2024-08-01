from django.core.management.base import BaseCommand
from quickstart.models import BusinessInfo

class Command(BaseCommand):
    help = 'Updates total reviews count for all businesses'

    def handle(self, *args, **options):
        businesses = BusinessInfo.objects.all()
        for business in businesses:
            business.update_total_reviews()
        self.stdout.write(self.style.SUCCESS('Successfully updated total reviews for all businesses'))