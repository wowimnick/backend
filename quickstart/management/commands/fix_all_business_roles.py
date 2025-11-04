# quickstart/management/commands/fix_all_business_roles.py

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Permission
from django.db import transaction
from quickstart.models import BusinessRole


class Command(BaseCommand):
    help = "Add access_business_dashboard permission to ALL BusinessRole instances"

    def handle(self, *args, **options):
        self.stdout.write("=" * 70)
        self.stdout.write(
            "FIXING ALL BUSINESS ROLES - Adding Dashboard Access Permission"
        )
        self.stdout.write("=" * 70)

        try:
            # Get the permission
            dashboard_perm = Permission.objects.get(
                codename="access_business_dashboard",
                content_type__app_label="quickstart",
            )

            self.stdout.write(
                self.style.SUCCESS(f"✓ Found permission: {dashboard_perm.codename}")
            )

            # Get ALL business roles (not just "Business Owner")
            all_roles = BusinessRole.objects.all().select_related("business")

            if not all_roles.exists():
                self.stdout.write(
                    self.style.WARNING("⚠ No BusinessRole instances found.")
                )
                return

            self.stdout.write(
                f"\nFound {all_roles.count()} BusinessRole(s) to check...\n"
            )

            updated_count = 0
            already_had_count = 0

            with transaction.atomic():
                for role in all_roles:
                    # Check if permission already exists
                    if not role.permissions.filter(id=dashboard_perm.id).exists():
                        role.permissions.add(dashboard_perm)
                        updated_count += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  ✓ ADDED: {role.name} ({role.business.businessName})"
                            )
                        )
                    else:
                        already_had_count += 1
                        self.stdout.write(
                            f"  - Already has permission: {role.name} ({role.business.businessName})"
                        )

            self.stdout.write("\n" + "=" * 70)
            self.stdout.write(
                self.style.SUCCESS(f"✓ COMPLETE: Updated {updated_count} role(s)")
            )
            self.stdout.write(
                f"  {already_had_count} role(s) already had the permission"
            )
            self.stdout.write("=" * 70)

            if updated_count > 0:
                self.stdout.write(
                    self.style.WARNING(
                        "\n⚠ IMPORTANT: Staff members need to log out and log back in "
                        "for permissions to take effect!"
                    )
                )

        except Permission.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(
                    "✗ ERROR: access_business_dashboard permission not found.\n"
                    "  Run 'python manage.py enhance_permissions' first."
                )
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"✗ ERROR: {str(e)}"))
