# quickstart/management/commands/enhance_permissions.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.db import transaction
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from ...models import PermissionGroup, EnhancedPermission

class Command(BaseCommand):
    help = 'Enhance ONLY explicitly defined permissions and organize them in groups'

    def handle(self, *args, **options):
        self.stdout.write('Enhancing explicitly defined permissions...')

        # Define which Django app content types to exclude COMPLETELY
        # (Keep this list as it helps filter the initial permission query)
        excluded_apps = [
            'admin', 'auth', 'contenttypes', 'sessions', 'sites',
            'admin_interface', 'theme', 'silk', 'allauth', 'account',
            'socialaccount', 'authtoken', 'token_blacklist',
            # Add other third-party apps if needed
        ]

        # Get content types to exclude based on app labels
        excluded_content_types = ContentType.objects.filter(app_label__in=excluded_apps)

        with transaction.atomic():
            # --- Create/Update Permission Groups ---
            self.stdout.write("Creating/Updating Permission Groups...")
            user_group, _ = PermissionGroup.objects.update_or_create(
                name="User Management", defaults={'description': 'Permissions related to user accounts and roles', 'sort_order': 1}
            )
            business_group, _ = PermissionGroup.objects.update_or_create(
                name="Business Management", defaults={'description': 'Permissions related to businesses', 'sort_order': 2}
            )
            class_group, _ = PermissionGroup.objects.update_or_create(
                name="Class Management", defaults={'description': 'Permissions related to classes, categories, and schedules', 'sort_order': 3}
            )
            booking_group, _ = PermissionGroup.objects.update_or_create(
                name="Booking Management", defaults={'description': 'Permissions related to bookings and attendance', 'sort_order': 4}
            )
            notification_group, _ = PermissionGroup.objects.update_or_create(
                name="Notification Management", defaults={'description': 'Permissions related to notifications and segments', 'sort_order': 5}
            )
            system_group, _ = PermissionGroup.objects.update_or_create(
                name="System & Moderation", defaults={'description': 'Permissions related to system settings, moderation, and administration', 'sort_order': 99}
            )

            # --- Define Explicit Descriptions & Custom Permissions ---
            # This dictionary is now the ONLY source for which permissions get enhanced.
            descriptions = {
                # --- User Management ---
                'view_customuser': {'group': user_group, 'description': 'View profile details for any user'},
                'change_customuser': {'group': user_group, 'description': 'Edit profile details (name, address, etc.) for any user', 'is_sensitive': True},
                'add_customuser': {'group': user_group, 'description': 'Create new user accounts via admin interface', 'is_sensitive': True},
                'delete_customuser': {'group': user_group, 'description': 'Delete any user account permanently', 'is_sensitive': True, 'requires_mfa': True},
                'change_user_role': {'group': user_group, 'description': 'Change the role assigned to any user (respects hierarchy)', 'is_sensitive': True},
                'lock_user': {'group': user_group, 'description': 'Lock or unlock any user account (respects hierarchy)', 'is_sensitive': True},
                'reset_user_password': {'group': user_group, 'description': 'Initiate password reset for any user (respects hierarchy)', 'is_sensitive': True},
                'view_user_metrics': {'group': user_group, 'description': 'View aggregated user management statistics'},
                'access_user_admin': {'group': user_group, 'description': 'General access to the User Administration section'},
                'view_role': {'group': user_group, 'description': 'View roles and their permissions'},
                'change_role': {'group': user_group, 'description': 'Edit roles and their permissions', 'is_sensitive': True},
                'add_role': {'group': user_group, 'description': 'Create new roles', 'is_sensitive': True},
                'delete_role': {'group': user_group, 'description': 'Delete roles (cannot delete system/assigned roles)', 'is_sensitive': True},

                # --- Business Management ---
                'view_businessinfo': {'group': business_group, 'description': 'View details for any business profile'},
                'change_businessinfo': {'group': business_group, 'description': 'Edit details for any business profile', 'is_sensitive': True},
                'add_businessinfo': {'group': business_group, 'description': 'Create new business profiles (Admin)', 'is_sensitive': True},
                'delete_businessinfo': {'group': business_group, 'description': 'Delete any business profile permanently', 'is_sensitive': True, 'requires_mfa': True},
                'toggle_business_feature': {'group': business_group, 'description': 'Toggle the featured status for any business'},
                'view_business_metrics': {'group': business_group, 'description': 'View aggregated business statistics and reports'},
                'export_business_data': {'group': business_group, 'description': 'Export business data as CSV', 'is_sensitive': True},
                'send_business_announcements': {'group': business_group, 'description': 'Send platform announcements to selected businesses'},
                'access_business_admin': {'group': business_group, 'description': 'General access to the Business Administration section'},
                'manage_own_classes': {'group': business_group, 'description': '(Business Role) Can create/edit classes, options, schedules for own business'},
                'manage_own_schedule_instances': {'group': business_group, 'description': '(Business Role) Can manage instances (attendance, cancel) for own classes'},
                'view_own_business_bookings': {'group': business_group, 'description': '(Business Role) Can view bookings for own business'},
                'manage_own_business_profile': {'group': business_group, 'description': '(Business Role) Can edit own business profile details'},
                'manage_business_staff': {'group': business_group, 'description': '(Business Role) Can manage staff for own business'},

                # --- Class Management ---
                'view_classesmain': {'group': class_group, 'description': 'View details for any class'},
                'change_classesmain': {'group': class_group, 'description': 'Edit details (title, category, etc.) for any class', 'is_sensitive': True},
                'add_classesmain': {'group': class_group, 'description': 'Create new classes via admin (if enabled)', 'is_sensitive': True},
                'delete_classesmain': {'group': class_group, 'description': 'Delete any class permanently', 'is_sensitive': True},
                'change_class_status': {'group': class_group, 'description': 'Change status (active/inactive/suspended) for any class', 'is_sensitive': True},
                'view_class_analytics': {'group': class_group, 'description': 'View aggregated class analytics'},
                'export_class_data': {'group': class_group, 'description': 'Export class data', 'is_sensitive': True},
                'access_class_admin': {'group': class_group, 'description': 'General access to the Class Administration section'},
                'view_classcategory': {'group': class_group, 'description': 'View class categories'},
                'add_classcategory': {'group': class_group, 'description': 'Create new class categories', 'is_sensitive': True},
                'change_classcategory': {'group': class_group, 'description': 'Edit class categories', 'is_sensitive': True},
                'delete_classcategory': {'group': class_group, 'description': 'Delete class categories', 'is_sensitive': True},
                'view_classsubcategory': {'group': class_group, 'description': 'View class subcategories'},
                'add_classsubcategory': {'group': class_group, 'description': 'Add class subcategories', 'is_sensitive': True},
                'change_classsubcategory': {'group': class_group, 'description': 'Edit class subcategories', 'is_sensitive': True},
                'delete_classsubcategory': {'group': class_group, 'description': 'Delete class subcategories', 'is_sensitive': True},
                'view_category_stats': {'group': class_group, 'description': 'View category statistics'},
                'access_category_admin': {'group': class_group, 'description': 'General access to the Category Administration section'},

                # --- Booking Management ---
                'view_booking': {'group': booking_group, 'description': 'View details for any booking', 'is_sensitive': True },
                'change_booking': {'group': booking_group, 'description': 'Modify details of any booking (Admin)', 'is_sensitive': True},
                'add_booking': {'group': booking_group, 'description': 'Create new bookings for any user (Admin)', 'is_sensitive': True},
                'delete_booking': {'group': booking_group, 'description': 'Delete any booking record (Use with caution)', 'is_sensitive': True},
                'cancel_any_booking': {'group': booking_group, 'description': 'Cancel any user\'s booking (Admin)', 'is_sensitive': True},
                'view_booking_analytics': {'group': booking_group, 'description': 'View aggregated booking analytics'},
                'export_booking_data': {'group': booking_group, 'description': 'Export booking data', 'is_sensitive': True},
                'access_booking_admin': {'group': booking_group, 'description': 'General access to Booking Administration'},
                'mark_booking_attendance': {'group': booking_group, 'description': 'Mark booking attendance'},

                # --- Notification Management ---
                'view_notificationcampaign': { 'group': notification_group, 'description': 'View notification campaign list and details'},
                'add_notificationcampaign': { 'group': notification_group, 'description': 'Create new notification campaigns'},
                'change_notificationcampaign': { 'group': notification_group, 'description': 'Edit notification campaigns (drafts/scheduled)', 'is_sensitive': True},
                'delete_notificationcampaign': { 'group': notification_group, 'description': 'Delete notification campaigns', 'is_sensitive': True},
                'send_notification_campaign': { 'group': notification_group, 'description': 'Trigger sending of campaigns', 'is_sensitive': True},
                'cancel_notification_campaign': { 'group': notification_group, 'description': 'Cancel scheduled campaigns'},
                'duplicate_notification_campaign': { 'group': notification_group, 'description': 'Duplicate existing campaigns'},
                'view_notification_metrics': { 'group': notification_group, 'description': 'View notification campaign metrics'},
                'access_notification_admin': { 'group': notification_group, 'description': 'General access to Notification Management'},
                'view_usersegment': { 'group': notification_group, 'description': 'View user segments'},
                'view_segment_users': { 'group': notification_group, 'description': 'View users within a specific segment', 'is_sensitive': True},
                'refresh_segment_counts': { 'group': notification_group, 'description': 'Trigger recalculation of segment counts'},
                'access_segment_admin': { 'group': notification_group, 'description': 'General access to User Segment Management'},
                'view_notificationattachment': { 'group': notification_group, 'description': 'View notification attachments'},
                'add_notificationattachment': { 'group': notification_group, 'description': 'Upload notification attachments'},
                # 'change_notificationattachment': { 'group': notification_group, 'description': 'Edit notification attachments'}, # Excluded change
                'delete_notificationattachment': { 'group': notification_group, 'description': 'Delete notification attachments', 'is_sensitive': True},

                # --- System & Moderation ---
                'view_payment': {'group': system_group, 'description': 'View details for any payment', 'is_sensitive': True},
                'delete_payment': {'group': system_group, 'description': 'Delete any payment record (Use with extreme caution)', 'is_sensitive': True, 'requires_mfa': True},
                'process_refund': {'group': system_group, 'description': 'Process refunds for any payment (Admin)', 'is_sensitive': True, 'requires_mfa': True },
                'mark_payment_paid': {'group': system_group, 'description': 'Manually mark a pending payment as paid (Admin)', 'is_sensitive': True},
                'view_payment_stats': {'group': system_group, 'description': 'View aggregated payment statistics'},
                'export_payment_data': {'group': system_group, 'description': 'Export payment data', 'is_sensitive': True},
                'access_payment_admin': {'group': system_group, 'description': 'General access to Payment Administration'},
                'view_reviews': {'group': system_group, 'description': 'View any user review'},
                'change_reviews': {'group': system_group, 'description': 'Moderate any review (status, response)', 'is_sensitive': True},
                'delete_reviews': {'group': system_group, 'description': 'Delete any user review permanently', 'is_sensitive': True},
                'access_review_admin': {'group': system_group, 'description': 'General access to the Review Moderation section'},
                'view_system_metrics': {'group': system_group, 'description': 'Can view real-time system performance metrics dashboard', 'is_sensitive': True},
                'view_auditlog': {'group': system_group, 'description': 'View audit logs of system activities', 'is_sensitive': True},
                'view_verificationrequest': {'group': system_group, 'description': 'View verification requests', 'is_sensitive': True},
                'change_verificationrequest': {'group': system_group, 'description': 'Process verification requests (approve/reject)', 'is_sensitive': True},
                'view_verificationdocument': {'group': system_group, 'description': 'View verification documents', 'is_sensitive': True},
                'view_supportticket': {'group': system_group, 'description': 'View any support ticket', 'is_sensitive': True},
                'change_supportticket': {'group': system_group, 'description': 'Manage any support ticket (assign, update status, reply)', 'is_sensitive': True},
                'delete_supportticket': {'group': system_group, 'description': 'Delete any support ticket', 'is_sensitive': True},
                'access_support_admin': {'group': system_group, 'description': 'General access to Support Ticket Administration'},
                'view_permissiongroup': {'group': system_group, 'description': 'View permission groups (Admin)'}, # Keep view for info
                'view_enhancedpermission': {'group': system_group, 'description': 'View enhanced permission details (Admin)'}, # Keep view for info
                # Add any other specific default permissions you WANT to manage here
                # e.g., 'view_studentnote', 'view_instructornote' if needed for admins

                # Support Tickets
                'view_supportticket': {'group': system_group, 'description': 'View any support ticket (Admin/Support)'},
                'change_supportticket': {'group': system_group, 'description': 'Modify any support ticket (Admin/Support - Use specific perms below)'},
                'add_supportticket': {'group': system_group, 'description': 'Create new support tickets (Users & Admins)'}, 
                'delete_supportticket': {'group': system_group, 'description': 'Delete any support ticket', 'is_sensitive': True},
                'reply_any_support_ticket': {'group': system_group, 'description': 'Reply to any support ticket (Admin/Agent)'},
                'assign_support_ticket': {'group': system_group, 'description': 'Assign any support ticket to an agent'},
                'resolve_support_ticket': {'group': system_group, 'description': 'Resolve or Close any support ticket'},
                'view_support_ticket_stats': {'group': system_group, 'description': 'View aggregated support ticket statistics'},
                'export_support_ticket_data': {'group': system_group, 'description': 'Export support ticket data', 'is_sensitive': True},
                'access_support_admin': {'group': system_group, 'description': 'General access to Support Ticket Administration'},
                # User-facing support permission
                'reply_own_support_ticket': {'group': user_group, 'description': 'Reply to own support tickets'}, 

            } # End descriptions dictionary

            # --- Enhance Permissions ---
            self.stdout.write("Enhancing explicitly defined permissions...")
            # 1. Delete all existing enhanced permissions
            deleted_count, _ = EnhancedPermission.objects.all().delete()
            if deleted_count > 0:
                self.stdout.write(f"Deleted {deleted_count} existing enhanced permissions.")

            # 2. Get permissions EXCLUDING those from irrelevant apps
            relevant_permissions = Permission.objects.exclude(content_type__in=excluded_content_types)
            self.stdout.write(f"Found {relevant_permissions.count()} potentially relevant permissions (pre-filter).")

            enhanced_count = 0
            processed_codenames = set()

            # 3. Process ONLY the permissions explicitly defined in the 'descriptions' dictionary
            for codename, attrs in descriptions.items():
                try:
                    # Find the permission object, ensuring it wasn't excluded by app label
                    perm = relevant_permissions.get(codename=codename)
                    EnhancedPermission.objects.create(
                        permission=perm,
                        group=attrs['group'],
                        description=attrs['description'],
                        is_sensitive=attrs.get('is_sensitive', False),
                        requires_mfa=attrs.get('requires_mfa', False)
                    )
                    processed_codenames.add(codename) # Mark as processed
                    enhanced_count += 1
                except Permission.DoesNotExist:
                    # This permission might be from an excluded app or misspelled in the dict
                    self.stdout.write(self.style.WARNING(f"Skipping: Permission '{codename}' listed in descriptions not found among relevant permissions or is excluded by app."))
                except Permission.MultipleObjectsReturned:
                     self.stdout.write(self.style.ERROR(f"DUPLICATE CODENAME '{codename}' found. Please clean up permissions in django.contrib.auth.models.Permission."))
                except KeyError as e:
                     self.stdout.write(self.style.ERROR(f"Configuration Error: Missing attribute {e} for permission '{codename}' in descriptions dict."))
                except Exception as e:
                     self.stdout.write(self.style.ERROR(f"Error processing explicit permission '{codename}': {e}"))


            self.stdout.write(f"Successfully processed and enhanced {enhanced_count} explicitly defined permissions.")
            # We no longer loop through remaining permissions. All others are ignored.

            # Optional: Log which potentially relevant permissions were ignored
            ignored_perms = relevant_permissions.exclude(codename__in=processed_codenames)
            ignored_count = ignored_perms.count()
            if ignored_count > 0:
                 self.stdout.write(f"Ignored {ignored_count} other default permissions (not explicitly defined in descriptions).")
                 # You could print them if needed for debugging:
                 # for p in ignored_perms:
                 #     print(f"  - Ignored: {p.content_type.app_label}.{p.codename}")