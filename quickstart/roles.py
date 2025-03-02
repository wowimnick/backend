from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from .models import Role, CustomUser, BusinessInfo, ClassesMain

def create_role(name, permissions):
    role, created = Role.objects.get_or_create(name=name)
    if created:
        role.permissions.set(permissions)
    return role

def setup_roles():
    # Get content types
    user_ct = ContentType.objects.get_for_model(CustomUser)
    business_ct = ContentType.objects.get_for_model(BusinessInfo)
    class_ct = ContentType.objects.get_for_model(ClassesMain)

    # Get specific permissions
    view_user = Permission.objects.get(content_type=user_ct, codename='view_customuser')
    change_user = Permission.objects.get(content_type=user_ct, codename='change_customuser')
    add_user = Permission.objects.get(content_type=user_ct, codename='add_customuser')
    
    view_class = Permission.objects.get(content_type=class_ct, codename='view_classesmain')
    change_class = Permission.objects.get(content_type=class_ct, codename='change_classesmain')
    add_class = Permission.objects.get(content_type=class_ct, codename='add_classesmain')
    
    view_business = Permission.objects.get(content_type=business_ct, codename='view_businessinfo')
    change_business = Permission.objects.get(content_type=business_ct, codename='change_businessinfo')
    add_business = Permission.objects.get(content_type=business_ct, codename='add_businessinfo')
    delete_business = Permission.objects.get(content_type=business_ct, codename='delete_businessinfo')

    # Student
    student_permissions = [
        view_user,
        view_class
    ]
    student_role = create_role('Student', student_permissions)
    student_role.color = "#10b981"  # Green
    student_role.hierarchy_level = 1
    student_role.save()

    # Instructor
    instructor_permissions = [
        view_user,
        view_class,
        change_class,
    ]
    instructor_role = create_role('Instructor', instructor_permissions)
    instructor_role.color = "#8b5cf6"  # Purple
    instructor_role.hierarchy_level = 2
    instructor_role.save()

    # Content Creator
    content_creator_permissions = instructor_permissions + [
        add_class
    ]
    content_creator_role = create_role('Content Creator', content_creator_permissions)
    content_creator_role.color = "#0ea5e9"  # Blue
    content_creator_role.hierarchy_level = 3
    content_creator_role.save()

    # Manager
    manager_permissions = content_creator_permissions + [
        add_user,
        change_user,
        view_business,
        change_business,
    ]
    manager_role = create_role('Manager', manager_permissions)
    manager_role.color = "#f97316"  # Orange
    manager_role.hierarchy_level = 4
    manager_role.save()

    # Business Owner
    business_owner_permissions = manager_permissions + [
        add_business,
        delete_business
    ]
    business_owner_role = create_role('Business Owner', business_owner_permissions)
    business_owner_role.color = "#3b82f6"  # Blue
    business_owner_role.hierarchy_level = 5
    business_owner_role.save()

    # Admin
    admin_permissions = Permission.objects.all()
    admin_role = create_role('Admin', admin_permissions)
    admin_role.color = "#ef4444"  # Red
    admin_role.hierarchy_level = 6
    admin_role.save()

    # Super Admin
    superadmin_permissions = Permission.objects.all()
    superadmin_role = create_role('Super Admin', superadmin_permissions)
    superadmin_role.color = "#dc2626"  # Dark red
    superadmin_role.hierarchy_level = 7
    superadmin_role.save()