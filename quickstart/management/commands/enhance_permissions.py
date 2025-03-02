# management/commands/enhance_permissions.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.db import transaction
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from ...models import PermissionGroup, EnhancedPermission, Role, CustomUser, BusinessInfo, ClassesMain

class Command(BaseCommand):
    help = 'Create user-friendly permission descriptions and organize them in groups'

    def handle(self, *args, **options):
        self.stdout.write('Creating permission groups and enhancing permissions...')
        
        # Define which Django app content types to exclude
        excluded_apps = [
            'admin', 
            'auth', 
            'contenttypes', 
            'sessions', 
            'sites',
            'theme',  
            'silk',
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
        
        with transaction.atomic():
            # Create permission groups
            user_group, _ = PermissionGroup.objects.get_or_create(
                name="User Management",
                defaults={
                    'description': 'Permissions related to user accounts and roles',
                    'sort_order': 1
                }
            )
            
            business_group, _ = PermissionGroup.objects.get_or_create(
                name="Business Management",
                defaults={
                    'description': 'Permissions related to businesses',
                    'sort_order': 2
                }
            )
            
            class_group, _ = PermissionGroup.objects.get_or_create(
                name="Class Management",
                defaults={
                    'description': 'Permissions related to classes and schedules',
                    'sort_order': 3
                }
            )
            
            booking_group, _ = PermissionGroup.objects.get_or_create(
                name="Booking Management",
                defaults={
                    'description': 'Permissions related to bookings and attendance',
                    'sort_order': 4
                }
            )
            
            system_group, _ = PermissionGroup.objects.get_or_create(
                name="System Management",
                defaults={
                    'description': 'Permissions related to system administration',
                    'sort_order': 5
                }
            )
            
            # Define friendly descriptions for some important permissions
            descriptions = {
                # User permissions
                'view_customuser': {
                    'group': user_group,
                    'description': 'View user profiles and account information',
                    'is_sensitive': False
                },
                'change_customuser': {
                    'group': user_group,
                    'description': 'Edit user information including personal details',
                    'is_sensitive': True
                },
                'add_customuser': {
                    'group': user_group,
                    'description': 'Create new user accounts',
                    'is_sensitive': True
                },
                'delete_customuser': {
                    'group': user_group,
                    'description': 'Delete user accounts permanently',
                    'is_sensitive': True,
                    'requires_mfa': True
                },
                
                # Role permissions
                'view_role': {
                    'group': user_group,
                    'description': 'View roles and their permissions',
                    'is_sensitive': False
                },
                'change_role': {
                    'group': user_group,
                    'description': 'Edit roles and their permissions',
                    'is_sensitive': True
                },
                'add_role': {
                    'group': user_group,
                    'description': 'Create new roles',
                    'is_sensitive': True
                },
                'delete_role': {
                    'group': user_group,
                    'description': 'Delete roles permanently',
                    'is_sensitive': True
                },
                
                # Business permissions
                'view_businessinfo': {
                    'group': business_group,
                    'description': 'View business information and details',
                    'is_sensitive': False
                },
                'change_businessinfo': {
                    'group': business_group,
                    'description': 'Edit business information and details',
                    'is_sensitive': False
                },
                'add_businessinfo': {
                    'group': business_group,
                    'description': 'Create new businesses',
                    'is_sensitive': False
                },
                'delete_businessinfo': {
                    'group': business_group,
                    'description': 'Delete businesses permanently',
                    'is_sensitive': True
                },
                
                # Class permissions
                'view_classesmain': {
                    'group': class_group,
                    'description': 'View classes and their details',
                    'is_sensitive': False
                },
                'change_classesmain': {
                    'group': class_group,
                    'description': 'Edit class information and details',
                    'is_sensitive': False
                },
                'add_classesmain': {
                    'group': class_group,
                    'description': 'Create new classes',
                    'is_sensitive': False
                },
                'delete_classesmain': {
                    'group': class_group,
                    'description': 'Delete classes permanently',
                    'is_sensitive': True
                },
                
                # Schedule permissions
                'view_schedule': {
                    'group': class_group,
                    'description': 'View class schedules',
                    'is_sensitive': False
                },
                'change_schedule': {
                    'group': class_group,
                    'description': 'Edit class schedules',
                    'is_sensitive': False
                },
                'add_schedule': {
                    'group': class_group,
                    'description': 'Create new class schedules',
                    'is_sensitive': False
                },
                
                # Schedule instance permissions
                'view_scheduleinstance': {
                    'group': class_group,
                    'description': 'View specific schedule instances',
                    'is_sensitive': False
                },
                'change_scheduleinstance': {
                    'group': class_group,
                    'description': 'Edit schedule instances',
                    'is_sensitive': False
                },
                
                # Booking permissions
                'view_booking': {
                    'group': booking_group,
                    'description': 'View bookings for classes',
                    'is_sensitive': False
                },
                'change_booking': {
                    'group': booking_group,
                    'description': 'Modify existing bookings',
                    'is_sensitive': False
                },
                'add_booking': {
                    'group': booking_group,
                    'description': 'Create new bookings',
                    'is_sensitive': False
                },
                
                # Attendance permissions
                'view_attendance': {
                    'group': booking_group,
                    'description': 'View attendance records',
                    'is_sensitive': False
                },
                'change_attendance': {
                    'group': booking_group,
                    'description': 'Mark attendance for students',
                    'is_sensitive': False
                },
                
                # Audit log permissions
                'view_auditlog': {
                    'group': system_group,
                    'description': 'View audit logs of system activities',
                    'is_sensitive': True
                },
                
                # Verification permissions
                'view_verificationrequest': {
                    'group': system_group,
                    'description': 'View verification requests',
                    'is_sensitive': True
                },
                'change_verificationrequest': {
                    'group': system_group,
                    'description': 'Process verification requests',
                    'is_sensitive': True
                },
            }
            
            # First, clear any existing enhanced permissions to avoid duplicates
            EnhancedPermission.objects.all().delete()
            
            # Get only relevant permissions (exclude Django internal)
            relevant_permissions = Permission.objects.exclude(
                content_type__in=excluded_content_types
            )
            
            self.stdout.write(f"Found {relevant_permissions.count()} relevant permissions")
            
            # Process permissions with specific descriptions
            for codename, attrs in descriptions.items():
                try:
                    perm = Permission.objects.get(codename=codename)
                    EnhancedPermission.objects.create(
                        permission=perm,
                        group=attrs['group'],
                        description=attrs['description'],
                        is_sensitive=attrs.get('is_sensitive', False),
                        requires_mfa=attrs.get('requires_mfa', False)
                    )
                    self.stdout.write(f"Enhanced permission: {codename}")
                except Permission.DoesNotExist:
                    self.stdout.write(self.style.WARNING(f"Permission not found: {codename}"))
            
            # Process remaining permissions without specific descriptions
            for perm in relevant_permissions.filter(~Q(codename__in=descriptions.keys())):
                model_name = perm.content_type.model
                action = perm.codename.split('_')[0]
                
                # Determine the appropriate group based on model name
                if model_name in ['customuser', 'role', 'profile']:
                    group = user_group
                elif model_name in ['businessinfo']:
                    group = business_group
                elif model_name in ['classesmain', 'classoption', 'classimage', 'schedule', 'scheduleinstance', 'schedulebreak']:
                    group = class_group
                elif model_name in ['booking', 'attendance', 'studentenrollment']:
                    group = booking_group
                else:
                    group = system_group
                
                # Create generic description
                action_desc = {
                    'view': 'View',
                    'add': 'Create new',
                    'change': 'Edit',
                    'delete': 'Delete'
                }.get(action, 'Manage')
                
                friendly_model_name = ' '.join(model_name.split('_')).title()
                description = f"{action_desc} {friendly_model_name}"
                
                EnhancedPermission.objects.create(
                    permission=perm,
                    group=group,
                    description=description,
                    is_sensitive=action in ['delete', 'add'],
                    requires_mfa=action == 'delete' and model_name in ['customuser', 'businessinfo']
                )
                
            self.stdout.write(self.style.SUCCESS(f"Enhanced {EnhancedPermission.objects.count()} permissions"))