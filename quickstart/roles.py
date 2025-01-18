from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from .models import Role, CustomUser, BusinessInfo, ClassesMain, Instructor

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
    instructor_ct = ContentType.objects.get_for_model(Instructor)

    # Get specific permissions
    view_user = Permission.objects.get(content_type=user_ct, codename='view_customuser')
    change_user = Permission.objects.get(content_type=user_ct, codename='change_customuser')
    add_user = Permission.objects.get(content_type=user_ct, codename='add_customuser')
    
    view_class = Permission.objects.get(content_type=class_ct, codename='view_classesmain')
    change_class = Permission.objects.get(content_type=class_ct, codename='change_classesmain')
    add_class = Permission.objects.get(content_type=class_ct, codename='add_classesmain')
    
    view_instructor = Permission.objects.get(content_type=instructor_ct, codename='view_instructor')
    change_instructor = Permission.objects.get(content_type=instructor_ct, codename='change_instructor')
    add_instructor = Permission.objects.get(content_type=instructor_ct, codename='add_instructor')
    
    view_business = Permission.objects.get(content_type=business_ct, codename='view_businessinfo')
    change_business = Permission.objects.get(content_type=business_ct, codename='change_businessinfo')
    add_business = Permission.objects.get(content_type=business_ct, codename='add_businessinfo')
    delete_business = Permission.objects.get(content_type=business_ct, codename='delete_businessinfo')

    # Student
    student_permissions = [
        view_user,
        view_class
    ]
    create_role('Student', student_permissions)

    # Instructor
    instructor_permissions = [
        view_user,
        view_class,
        change_class,
        view_instructor,
        change_instructor
    ]
    create_role('Instructor', instructor_permissions)

    # Content Creator
    content_creator_permissions = instructor_permissions + [
        add_class
    ]
    create_role('Content Creator', content_creator_permissions)

    # Manager
    manager_permissions = content_creator_permissions + [
        add_user,
        change_user,
        view_business,
        change_business,
        add_instructor
    ]
    create_role('Manager', manager_permissions)

    # Business Owner
    business_owner_permissions = manager_permissions + [
        add_business,
        delete_business
    ]
    create_role('Business Owner', business_owner_permissions)

    # Admin
    admin_permissions = Permission.objects.all()
    create_role('Admin', admin_permissions)

    # SuperAdmin
    superadmin_permissions = Permission.objects.all()
    create_role('SuperAdmin', superadmin_permissions)