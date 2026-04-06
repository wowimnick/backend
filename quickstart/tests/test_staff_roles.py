"""Staff roles and permission backend."""
import pytest
from django.contrib.auth.models import Permission

from quickstart.backends import RolePermissionBackend
from quickstart.tests.factories import BusinessRoleFactory, BusinessStaffFactory, UserFactory


@pytest.mark.django_db
class TestRolePermissionBackend:
    def test_superuser_has_any_perm(self):
        backend = RolePermissionBackend()
        admin = UserFactory(is_superuser=True)
        assert backend.has_perm(admin, "quickstart.access_admin_dashboard")

    def test_staff_role_grants_business_permission(self):
        backend = RolePermissionBackend()
        user = UserFactory()
        staff = BusinessStaffFactory(user=user, status="accepted")
        perm = Permission.objects.filter(
            content_type__app_label="quickstart",
            codename="access_business_dashboard",
        ).first()
        if not perm:
            pytest.skip("quickstart.access_business_dashboard permission missing")
        staff.role.permissions.add(perm)
        assert backend.has_perm(user, "quickstart.access_business_dashboard")

    def test_plain_user_denied_unknown_perm(self):
        backend = RolePermissionBackend()
        user = UserFactory()
        assert not backend.has_perm(user, "quickstart.access_admin_dashboard")
