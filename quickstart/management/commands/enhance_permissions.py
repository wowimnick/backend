# quickstart/management/commands/enhance_permissions.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.db import transaction
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from ...models import PermissionGroup, EnhancedPermission


class Command(BaseCommand):
    help = "Enhance ONLY explicitly defined permissions and organize them in groups"

    def handle(self, *args, **options):
        self.stdout.write("Enhancing explicitly defined permissions...")

        # Define which Django app content types to exclude COMPLETELY
        excluded_apps = [
            "admin",
            "auth",
            "contenttypes",
            "sessions",
            "sites",
            "admin_interface",
            "theme",
            "allauth",
            "account",
            "socialaccount",
            "authtoken",
            "token_blacklist",
        ]

        # Get content types to exclude based on app labels
        excluded_content_types = ContentType.objects.filter(app_label__in=excluded_apps)

        with transaction.atomic():
            # --- Create/Update Permission Groups ---
            self.stdout.write("Creating/Updating Permission Groups...")

            # --- PLATFORM ADMIN GROUPS (For main admin panel) ---
            user_group, _ = PermissionGroup.objects.update_or_create(
                name="User Management",
                defaults={
                    "description": "Permissions related to user accounts and roles",
                    "sort_order": 1,
                    "ui_category": None,  # Ensure this is not a business UI group
                },
            )
            business_group, _ = PermissionGroup.objects.update_or_create(
                name="Business Admin",
                defaults={
                    "description": "Permissions related to platform-wide business administration",
                    "sort_order": 2,
                    "ui_category": None,
                },
            )
            class_group, _ = PermissionGroup.objects.update_or_create(
                name="Class Admin",
                defaults={
                    "description": "Permissions related to platform-wide class/category administration",
                    "sort_order": 3,
                    "ui_category": None,
                },
            )
            booking_group, _ = PermissionGroup.objects.update_or_create(
                name="Booking Admin",
                defaults={
                    "description": "Permissions related to platform-wide booking administration",
                    "sort_order": 4,
                    "ui_category": None,
                },
            )
            payout_group, _ = PermissionGroup.objects.update_or_create(
                name="Payout Admin",
                defaults={
                    "description": "Permissions related to business payouts and financial transfers.",
                    "sort_order": 5,
                    "ui_category": None,
                },
            )
            notification_group, _ = PermissionGroup.objects.update_or_create(
                name="Notification Management",
                defaults={
                    "description": "Permissions related to notifications and segments",
                    "sort_order": 6,
                    "ui_category": None,
                },
            )
            content_group, _ = PermissionGroup.objects.update_or_create(
                name="Content Management",
                defaults={
                    "description": "Permissions related to managing blog posts, categories, and other site content.",
                    "sort_order": 7,
                    "ui_category": None,
                },
            )
            system_group, _ = PermissionGroup.objects.update_or_create(
                name="System & Moderation",
                defaults={
                    "description": "Permissions related to system settings, moderation, and administration",
                    "sort_order": 99,
                    "ui_category": None,
                },
            )

            # --- NEW: BUSINESS UI GROUPS (for Role Editor Tabs) ---
            self.stdout.write("Creating/Updating Business UI Permission Groups...")
            biz_profile_staff_group, _ = PermissionGroup.objects.update_or_create(
                name="Profile & Staff",
                defaults={"sort_order": 10, "ui_category": "business_role_editor"},
            )
            biz_classes_schedule_group, _ = PermissionGroup.objects.update_or_create(
                name="Classes & Schedule",
                defaults={"sort_order": 11, "ui_category": "business_role_editor"},
            )
            biz_finance_analytics_group, _ = PermissionGroup.objects.update_or_create(
                name="Finance & Analytics",
                defaults={"sort_order": 12, "ui_category": "business_role_editor"},
            )
            biz_student_engagement_group, _ = PermissionGroup.objects.update_or_create(
                name="Student Engagement",
                defaults={"sort_order": 13, "ui_category": "business_role_editor"},
            )

            # --- Define Explicit Descriptions & Custom Permissions ---
            # This dictionary is the ONLY source for which permissions get enhanced.
            descriptions = {
                # --- User Management (Platform Admins) ---
                "view_customuser": {
                    "group": user_group,
                    "description": "View profile details for any user (Admin)",
                },
                "change_customuser": {
                    "group": user_group,
                    "description": "Edit profile details (name, address, etc.) for any user",
                    "is_sensitive": True,
                },
                "add_customuser": {
                    "group": user_group,
                    "description": "Create new user accounts via admin interface",
                    "is_sensitive": True,
                },
                "delete_customuser": {
                    "group": user_group,
                    "description": "Delete any user account permanently",
                    "is_sensitive": True,
                    "requires_mfa": True,
                },
                "change_user_role": {
                    "group": user_group,
                    "description": "Change the role assigned to any user (respects hierarchy)",
                    "is_sensitive": True,
                },
                "lock_user": {
                    "group": user_group,
                    "description": "Lock or unlock any user account (respects hierarchy)",
                    "is_sensitive": True,
                },
                "reset_user_password": {
                    "group": user_group,
                    "description": "Initiate password reset for any user (respects hierarchy)",
                    "is_sensitive": True,
                },
                "view_user_metrics": {
                    "group": user_group,
                    "description": "View aggregated user management statistics",
                },
                "access_user_admin": {
                    "group": user_group,
                    "description": "General access to the User Administration section",
                },
                "view_role": {
                    "group": user_group,
                    "description": "View roles and their permissions",
                },
                "change_role": {
                    "group": user_group,
                    "description": "Edit roles and their permissions",
                    "is_sensitive": True,
                },
                "add_role": {
                    "group": user_group,
                    "description": "Create new roles",
                    "is_sensitive": True,
                },
                "delete_role": {
                    "group": user_group,
                    "description": "Delete roles (cannot delete system/assigned roles)",
                    "is_sensitive": True,
                },
                # --- User Permissions (Students/General Users) ---
                "reply_own_support_ticket": {
                    "group": user_group,
                    "description": "Reply to own support tickets",
                },
                "cancel_own_booking": {
                    "group": user_group,
                    "description": "Cancel own booking (within policy)",
                },
                "add_supportticket": {
                    "group": user_group,
                    "description": "Create new support tickets",
                },
                # --- Business Admin (Platform Admins) ---
                "view_businessinfo": {
                    "group": business_group,
                    "description": "View details for any business profile",
                },
                "change_businessinfo": {
                    "group": business_group,
                    "description": "Edit details for any business profile",
                    "is_sensitive": True,
                },
                "add_businessinfo": {
                    "group": business_group,
                    "description": "Create new business profiles (Admin)",
                    "is_sensitive": True,
                },
                "delete_businessinfo": {
                    "group": business_group,
                    "description": "Delete any business profile permanently",
                    "is_sensitive": True,
                    "requires_mfa": True,
                },
                "toggle_business_feature": {
                    "group": business_group,
                    "description": "Toggle the featured status for any business",
                },
                "view_business_metrics": {
                    "group": business_group,
                    "description": "View aggregated business statistics and reports",
                },
                "export_business_data": {
                    "group": business_group,
                    "description": "Export business data as CSV",
                    "is_sensitive": True,
                },
                "send_business_announcements": {
                    "group": business_group,
                    "description": "Send platform announcements to selected businesses",
                },
                "access_business_admin": {
                    "group": business_group,
                    "description": "General access to the Business Administration section",
                },
                "view_all_verificationrequests": {
                    "group": business_group,
                    "description": "View all verification requests (Admin)",
                },
                "process_verificationrequest": {
                    "group": business_group,
                    "description": "Process verification requests (approve/reject) for any business",
                    "is_sensitive": True,
                },
                # --- Class Admin (Platform Admins) ---
                "view_classesmain": {
                    "group": class_group,
                    "description": "View details for any class",
                },
                "change_classesmain": {
                    "group": class_group,
                    "description": "Edit details (title, category, etc.) for any class",
                    "is_sensitive": True,
                },
                "add_classesmain": {
                    "group": class_group,
                    "description": "Create new classes via admin (if enabled)",
                    "is_sensitive": True,
                },
                "delete_classesmain": {
                    "group": class_group,
                    "description": "Delete any class permanently",
                    "is_sensitive": True,
                },
                "change_class_status": {
                    "group": class_group,
                    "description": "Change status (active/inactive/suspended) for any class",
                    "is_sensitive": True,
                },
                "view_class_analytics": {
                    "group": class_group,
                    "description": "View aggregated class analytics",
                },
                "export_class_data": {
                    "group": class_group,
                    "description": "Export class data",
                    "is_sensitive": True,
                },
                "access_class_admin": {
                    "group": class_group,
                    "description": "General access to the Class Administration section",
                },
                "view_classcategory": {
                    "group": class_group,
                    "description": "View class categories",
                },
                "add_classcategory": {
                    "group": class_group,
                    "description": "Create new class categories",
                    "is_sensitive": True,
                },
                "change_classcategory": {
                    "group": class_group,
                    "description": "Edit class categories",
                    "is_sensitive": True,
                },
                "delete_classcategory": {
                    "group": class_group,
                    "description": "Delete class categories",
                    "is_sensitive": True,
                },
                "view_classsubcategory": {
                    "group": class_group,
                    "description": "View class subcategories",
                },
                "add_classsubcategory": {
                    "group": class_group,
                    "description": "Add class subcategories",
                    "is_sensitive": True,
                },
                "change_classsubcategory": {
                    "group": class_group,
                    "description": "Edit class subcategories",
                    "is_sensitive": True,
                },
                "delete_classsubcategory": {
                    "group": class_group,
                    "description": "Delete class subcategories",
                    "is_sensitive": True,
                },
                "view_category_stats": {
                    "group": class_group,
                    "description": "View category statistics",
                },
                "access_category_admin": {
                    "group": class_group,
                    "description": "General access to the Category Administration section",
                },
                # --- Booking Admin (Platform Admins) ---
                "view_booking": {
                    "group": booking_group,
                    "description": "View details for any booking",
                    "is_sensitive": True,
                },
                "change_booking": {
                    "group": booking_group,
                    "description": "Modify details of any booking (Admin)",
                    "is_sensitive": True,
                },
                "add_booking": {
                    "group": booking_group,
                    "description": "Create new bookings for any user (Admin)",
                    "is_sensitive": True,
                },
                "delete_booking": {
                    "group": booking_group,
                    "description": "Delete any booking record (Use with caution)",
                    "is_sensitive": True,
                },
                "cancel_any_booking": {
                    "group": booking_group,
                    "description": "Cancel any user's booking (Admin)",
                    "is_sensitive": True,
                },
                "view_booking_analytics": {
                    "group": booking_group,
                    "description": "View aggregated booking analytics",
                },
                "export_booking_data": {
                    "group": booking_group,
                    "description": "Export booking data",
                    "is_sensitive": True,
                },
                "access_booking_admin": {
                    "group": booking_group,
                    "description": "General access to Booking Administration",
                },
                # --- Notification Management (Platform Admins) ---
                "view_notificationcampaign": {
                    "group": notification_group,
                    "description": "View notification campaign list and details",
                },
                "add_notificationcampaign": {
                    "group": notification_group,
                    "description": "Create new notification campaigns",
                },
                "change_notificationcampaign": {
                    "group": notification_group,
                    "description": "Edit notification campaigns (drafts/scheduled)",
                    "is_sensitive": True,
                },
                "delete_notificationcampaign": {
                    "group": notification_group,
                    "description": "Delete notification campaigns",
                    "is_sensitive": True,
                },
                "send_notification_campaign": {
                    "group": notification_group,
                    "description": "Trigger sending of campaigns",
                    "is_sensitive": True,
                },
                "cancel_notification_campaign": {
                    "group": notification_group,
                    "description": "Cancel scheduled campaigns",
                },
                "duplicate_notification_campaign": {
                    "group": notification_group,
                    "description": "Duplicate existing campaigns",
                },
                "view_notification_metrics": {
                    "group": notification_group,
                    "description": "View notification campaign metrics",
                },
                "access_notification_admin": {
                    "group": notification_group,
                    "description": "General access to Notification Management",
                },
                "view_usersegment": {
                    "group": notification_group,
                    "description": "View user segments",
                },
                "view_segment_users": {
                    "group": notification_group,
                    "description": "View users within a specific segment",
                    "is_sensitive": True,
                },
                "refresh_segment_counts": {
                    "group": notification_group,
                    "description": "Trigger recalculation of segment counts",
                },
                "access_segment_admin": {
                    "group": notification_group,
                    "description": "General access to User Segment Management",
                },
                "view_notificationattachment": {
                    "group": notification_group,
                    "description": "View notification attachments",
                },
                "add_notificationattachment": {
                    "group": notification_group,
                    "description": "Upload notification attachments",
                },
                "delete_notificationattachment": {
                    "group": notification_group,
                    "description": "Delete notification attachments",
                    "is_sensitive": True,
                },
                # --- Payout Admin (Platform Admins) ---
                "access_payout_admin": {
                    "group": payout_group,
                    "description": "General access to the Payout Administration section",
                },
                "view_payout_analytics": {
                    "group": payout_group,
                    "description": "View aggregated payout statistics and reports",
                },
                "export_payout_data": {
                    "group": payout_group,
                    "description": "Export payout and included booking data",
                    "is_sensitive": True,
                },
                "trigger_manual_payout": {
                    "group": payout_group,
                    "description": "Manually trigger the payout generation process",
                    "is_sensitive": True,
                },
                "retry_failed_payout": {
                    "group": payout_group,
                    "description": "Retry a payout transfer that has previously failed",
                    "is_sensitive": True,
                },
                "view_payout": {
                    "group": payout_group,
                    "description": "View details for any payout, including included bookings",
                    "is_sensitive": True,
                },
                "delete_payout": {
                    "group": payout_group,
                    "description": "Delete a payout record (Use with extreme caution)",
                    "is_sensitive": True,
                    "requires_mfa": True,
                },
                # --- Content Management (Platform Admins) ---
                "manage_blog_posts": {
                    "group": content_group,
                    "description": "Can create, edit, publish, and delete blog posts.",
                    "is_sensitive": True,
                },
                "manage_blog_categories": {
                    "group": content_group,
                    "description": "Can create, edit, and delete blog categories.",
                    "is_sensitive": True,
                },
                "access_blog_admin": {
                    "group": content_group,
                    "description": "General access to the Blog Management section in the admin panel.",
                },
                "access_global_discount_admin": {
                    "group": content_group,
                    "description": "Manage platform-wide global discounts (create, edit, delete, view stats).",
                },
                # --- System & Moderation (Platform Admins) ---
                "access_admin_dashboard": {
                    "group": system_group,
                    "description": "Access the main admin dashboard",
                },
                "view_system_metrics": {
                    "group": system_group,
                    "description": "Can view real-time system performance metrics dashboard",
                    "is_sensitive": True,
                },
                "view_auditlog": {
                    "group": system_group,
                    "description": "View audit logs of system activities",
                    "is_sensitive": True,
                },
                "view_verificationrequest": {
                    "group": system_group,
                    "description": "View verification requests",
                    "is_sensitive": True,
                },
                "change_verificationrequest": {
                    "group": system_group,
                    "description": "Process verification requests (approve/reject)",
                    "is_sensitive": True,
                },
                "view_verificationdocument": {
                    "group": system_group,
                    "description": "View verification documents",
                    "is_sensitive": True,
                },
                "view_payment": {
                    "group": system_group,
                    "description": "View details for any payment",
                    "is_sensitive": True,
                },
                "delete_payment": {
                    "group": system_group,
                    "description": "Delete any payment record (Use with extreme caution)",
                    "is_sensitive": True,
                    "requires_mfa": True,
                },
                "process_refund": {
                    "group": system_group,
                    "description": "Process refunds for any payment (Admin)",
                    "is_sensitive": True,
                    "requires_mfa": True,
                },
                "mark_payment_paid": {
                    "group": system_group,
                    "description": "Manually mark a pending payment as paid (Admin)",
                    "is_sensitive": True,
                },
                "view_payment_stats": {
                    "group": system_group,
                    "description": "View aggregated payment statistics",
                },
                "export_payment_data": {
                    "group": system_group,
                    "description": "Export payment data",
                    "is_sensitive": True,
                },
                "access_payment_admin": {
                    "group": system_group,
                    "description": "General access to Payment Administration",
                },
                "view_reviews": {
                    "group": system_group,
                    "description": "View any user review",
                },
                "change_reviews": {
                    "group": system_group,
                    "description": "Moderate any review (status, response)",
                    "is_sensitive": True,
                },
                "delete_reviews": {
                    "group": system_group,
                    "description": "Delete any user review permanently",
                    "is_sensitive": True,
                },
                "access_review_admin": {
                    "group": system_group,
                    "description": "General access to the Review Moderation section",
                },
                "view_supportticket": {
                    "group": system_group,
                    "description": "View any support ticket (Admin/Support)",
                },
                "change_supportticket": {
                    "group": system_group,
                    "description": "Modify any support ticket (Admin/Support - Use specific perms below)",
                },
                "delete_supportticket": {
                    "group": system_group,
                    "description": "Delete any support ticket",
                    "is_sensitive": True,
                },
                "assign_support_ticket": {
                    "group": system_group,
                    "description": "Assign any support ticket to an agent",
                },
                "view_support_ticket_stats": {
                    "group": system_group,
                    "description": "View aggregated support ticket statistics",
                },
                "export_support_ticket_data": {
                    "group": system_group,
                    "description": "Export support ticket data",
                    "is_sensitive": True,
                },
                "access_support_admin": {
                    "group": system_group,
                    "description": "General access to Support Ticket Administration",
                },
                # --- UPDATED: Business User Permissions (Assigned to new UI Groups) ---
                "access_business_dashboard": {
                    "group": biz_profile_staff_group,
                    "description": "Grants basic access to the business dashboard. Required for all staff members.",
                },
                "manage_own_business_profile": {
                    "group": biz_profile_staff_group,
                    "description": "Allows user to edit the main business profile details, including name, description, contact info, and images.",
                },
                "manage_business_staff": {
                    "group": biz_profile_staff_group,
                    "description": "Allows user to invite new staff, remove existing staff, and change their assigned roles.",
                    "is_sensitive": True,
                },
                "manage_business_roles": {
                    "group": biz_profile_staff_group,
                    "description": "Can create, edit, and delete staff roles and assign permissions.",
                    "is_sensitive": True,
                },
                "manage_own_classes": {
                    "group": biz_classes_schedule_group,
                    "description": "Full control over creating and editing classes, class options, pricing, and recurring schedules.",
                },
                "manage_own_schedule_instances": {
                    "group": biz_classes_schedule_group,
                    "description": "Allows user to manage individual class sessions, such as canceling a single session or marking attendance.",
                },
                "manage_own_business_discounts": {
                    "group": biz_classes_schedule_group,
                    "description": "Allows user to create, edit, and manage discounts and coupon codes for their business.",
                },
                "view_business_revenue_analytics": {
                    "group": biz_finance_analytics_group,
                    "description": "Allows user to view detailed revenue reports, sales trends, and payout information.",
                },
                "export_business_revenue_data": {
                    "group": biz_finance_analytics_group,
                    "description": "Allows user to export financial and revenue data for accounting purposes.",
                },
                "view_own_booking_analytics": {
                    "group": biz_finance_analytics_group,
                    "description": "Allows user to view analytics related to booking trends, popular classes, and student attendance.",
                },
                "view_own_business_bookings": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to see a list of all current and past bookings for the business.",
                },
                "cancel_business_booking": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to cancel a student's booking on their behalf.",
                },
                "view_business_students": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to view the list of students who have booked classes with the business.",
                },
                "view_studentnote": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to view private notes about students, left by other staff members.",
                },
                "add_studentnote": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to add private notes to a student's profile for internal reference.",
                },
                "view_own_business_reviews": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to read all reviews submitted by students for the business's classes.",
                },
                "add_business_review_response": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to write and publish public responses to student reviews.",
                },
                "receive_booking_notifications": {
                    "group": biz_student_engagement_group,
                    "description": "Staff with this permission will receive email notifications for new bookings and cancellations.",
                },
                "manage_email_marketing": {
                    "group": biz_student_engagement_group,
                    "description": "Allows user to create, edit, schedule, and send email marketing campaigns for their business. Grants access to the Email Campaigns tab independently of class management.",
                },
            }

            # --- Enhance Permissions ---
            self.stdout.write("Enhancing explicitly defined permissions...")
            deleted_count, _ = EnhancedPermission.objects.all().delete()
            if deleted_count > 0:
                self.stdout.write(
                    f"Deleted {deleted_count} existing enhanced permissions."
                )

            relevant_permissions = Permission.objects.exclude(
                content_type__in=excluded_content_types
            )
            self.stdout.write(
                f"Found {relevant_permissions.count()} potentially relevant permissions (pre-filter)."
            )

            enhanced_count = 0
            processed_codenames = set()

            for codename, attrs in descriptions.items():
                if codename in processed_codenames:
                    continue  # Avoid processing duplicates
                try:
                    perm = relevant_permissions.get(codename=codename)
                    EnhancedPermission.objects.create(
                        permission=perm,
                        group=attrs["group"],
                        description=attrs["description"],
                        is_sensitive=attrs.get("is_sensitive", False),
                        requires_mfa=attrs.get("requires_mfa", False),
                    )
                    processed_codenames.add(codename)
                    enhanced_count += 1
                except Permission.DoesNotExist:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Skipping: Permission '{codename}' not found among relevant permissions."
                        )
                    )
                except Permission.MultipleObjectsReturned:
                    self.stdout.write(
                        self.style.ERROR(
                            f"DUPLICATE CODENAME '{codename}' found. Please clean up permissions."
                        )
                    )
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(
                            f"Error processing permission '{codename}': {e}"
                        )
                    )

            self.stdout.write(
                f"Successfully processed and enhanced {enhanced_count} explicitly defined permissions."
            )

        self.stdout.write(self.style.SUCCESS("Permission enhancement complete."))
