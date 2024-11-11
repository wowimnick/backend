from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from .models import Role, CustomUser, BusinessInfo, ClassesMain, Instructor

def create_role(name, permissions):
    role, created = Role.objects.get_or_create(name=name)
    if created:
        role.permissions.set(permissions)
    return role

def setup_roles():
    # Define permissions for each model
    user_permissions = Permission.objects.filter(content_type=ContentType.objects.get_for_model(CustomUser))
    business_permissions = Permission.objects.filter(content_type=ContentType.objects.get_for_model(BusinessInfo))
    class_permissions = Permission.objects.filter(content_type=ContentType.objects.get_for_model(ClassesMain))
    instructor_permissions = Permission.objects.filter(content_type=ContentType.objects.get_for_model(Instructor))

    # Guest (Not logged in)
    # No need to create a role for guests, as they will have no permissions

    # Student
    student_permissions = [
        user_permissions.get(codename='view_customuser'),
        class_permissions.get(codename='view_classesmain'),
    ]
    create_role('Student', student_permissions)

    # Instructor
    instructor_permissions = [
        user_permissions.get(codename='view_customuser'),
        class_permissions.get(codename='view_classesmain'),
        class_permissions.get(codename='change_classesmain'),
        instructor_permissions.get(codename='view_instructor'),
        instructor_permissions.get(codename='change_instructor'),
    ]
    create_role('Instructor', instructor_permissions)

    # Content Creator
    content_creator_permissions = instructor_permissions + [
        class_permissions.get(codename='add_classesmain'),
    ]
    create_role('Content Creator', content_creator_permissions)

    # Manager
    manager_permissions = content_creator_permissions + [
        user_permissions.get(codename='add_customuser'),
        user_permissions.get(codename='change_customuser'),
        business_permissions.get(codename='view_businessinfo'),
        business_permissions.get(codename='change_businessinfo'),
        instructor_permissions.get(codename='add_instructor'),
    ]
    create_role('Manager', manager_permissions)

    # Business Owner
    business_owner_permissions = manager_permissions + [
        business_permissions.get(codename='add_businessinfo'),
        business_permissions.get(codename='delete_businessinfo'),
    ]
    create_role('Business Owner', business_owner_permissions)

    # Admin (Platform)
    admin_permissions = Permission.objects.all()
    create_role('Admin', admin_permissions)

    # SuperAdmin (Platform)
    superadmin_permissions = Permission.objects.all()
    create_role('SuperAdmin', superadmin_permissions)