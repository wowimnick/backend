# management/commands/clean_permissions.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from ...models import EnhancedPermission, Role

class Command(BaseCommand):
    help = 'Clean up enhanced permissions by removing Django internal ones'
    
    def handle(self, *args, **options):
        # Define which Django app content types to exclude
        excluded_apps = [
            'admin', 
            'theme',
            'admin_interface',
            'allauth',          
            'account',          
            'socialaccount',  
            'rest_framework.authtoken', 
            'authtoken',      
            'token_blacklist', 
            'auth', 
            'contenttypes', 
            'sessions', 
            'sites',
            'theme',  
            'allauth',
            'account',
            'socialaccount',
            'token',
            'tokenproxy',
            'blacklistedtoken',
            'outstandingtoken'
        ]
        # Get content types to exclude
        excluded_content_types = ContentType.objects.filter(app_label__in=excluded_apps)
        
        # Get the enhanced permissions to remove
        to_remove = EnhancedPermission.objects.filter(
            permission__content_type__in=excluded_content_types
        )
        
        count = to_remove.count()
        to_remove.delete()
        
        self.stdout.write(self.style.SUCCESS(f'Successfully removed {count} Django internal permissions'))
        
        # Also remove these permissions from roles
        excluded_permissions = Permission.objects.filter(
            content_type__in=excluded_content_types
        )
        
        self.stdout.write('Cleaning up role permissions...')
        
        # For each role, remove the excluded permissions
        for role in Role.objects.all():
            before_count = role.permissions.count()
            role.permissions.remove(*excluded_permissions)
            after_count = role.permissions.count()
            removed = before_count - after_count
            if removed > 0:
                self.stdout.write(f'Removed {removed} internal permissions from role "{role.name}"')
        
        self.stdout.write(self.style.SUCCESS('Permission cleanup complete'))