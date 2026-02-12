# Add access_global_discount_admin permission so enhance_permissions can find it.
# Django creates custom permissions from Model.Meta.permissions at migrate time,
# but this permission was added to CustomUser.Meta after the last AlterCustomUserOptions
# migration, so we ensure it exists here.

from django.db import migrations


def add_global_discount_admin_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ct = ContentType.objects.get(app_label="quickstart", model="customuser")
    Permission.objects.get_or_create(
        codename="access_global_discount_admin",
        content_type=ct,
        defaults={"name": "Can access Global Discount management"},
    )


def remove_global_discount_admin_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ct = ContentType.objects.get(app_label="quickstart", model="customuser")
    Permission.objects.filter(
        codename="access_global_discount_admin",
        content_type=ct,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0193_global_discount_models"),
    ]

    operations = [
        migrations.RunPython(add_global_discount_admin_permission, remove_global_discount_admin_permission),
    ]
