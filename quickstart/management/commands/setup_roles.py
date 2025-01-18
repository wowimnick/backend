from django.core.management.base import BaseCommand
from quickstart.roles import setup_roles

class Command(BaseCommand):
    help = 'Sets up initial roles and permissions'

    def handle(self, *args, **kwargs):
        setup_roles()
        self.stdout.write(self.style.SUCCESS('Successfully set up roles and permissions'))