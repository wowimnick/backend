# management/commands/reset_permissions.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from ...models import Role, EnhancedPermission, PermissionGroup

class Command(BaseCommand):
    help = 'Reset permissions and roles to their default state'
    
    def handle(self, *args, **options):
        self.stdout.write('Resetting permissions and roles...')
        
        # 1. Clear enhanced permissions and groups
        self.stdout.write('Clearing enhanced permissions and groups...')
        EnhancedPermission.objects.all().delete()
        PermissionGroup.objects.all().delete()
        
        # 2. Reset roles to default settings
        # We'll keep user-created roles but reset their permissions
        # Removing only non-system roles would be dangerous in production
        self.stdout.write('Resetting role permissions...')
        for role in Role.objects.all():
            role.permissions.clear()
        
        # 3. Re-run setup_roles function to recreate roles with correct permissions
        self.stdout.write('Setting up default roles and permissions...')
        from ...roles import setup_roles
        setup_roles()
        
        # 4. Update all roles with proper colors and hierarchy levels
        self.stdout.write('Updating role colors and hierarchy levels...')
        roles_to_update = {
            'Student': {'color': '#10b981', 'hierarchy_level': 1},
            'Instructor': {'color': '#8b5cf6', 'hierarchy_level': 2},
            'Content Creator': {'color': '#0ea5e9', 'hierarchy_level': 3},
            'Manager': {'color': '#f97316', 'hierarchy_level': 4},
            'Business Owner': {'color': '#3b82f6', 'hierarchy_level': 5},
            'Admin': {'color': '#ef4444', 'hierarchy_level': 6},
            'Super Admin': {'color': '#dc2626', 'hierarchy_level': 7}
        }
        
        for role_name, attrs in roles_to_update.items():
            try:
                role = Role.objects.get(name=role_name)
                role.color = attrs['color']
                role.hierarchy_level = attrs['hierarchy_level']
                role.save()
                self.stdout.write(f'Updated role: {role_name}')
            except Role.DoesNotExist:
                self.stdout.write(self.style.WARNING(f'Role not found: {role_name}'))
        
        self.stdout.write(self.style.SUCCESS('Successfully reset permissions and roles'))
        self.stdout.write('Now, run "python manage.py enhance_permissions" to add user-friendly descriptions')