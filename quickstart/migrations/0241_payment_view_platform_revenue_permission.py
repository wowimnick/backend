from django.db import migrations


def add_view_platform_revenue_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ct = ContentType.objects.get(app_label="quickstart", model="payment")
    Permission.objects.get_or_create(
        codename="view_platform_revenue",
        content_type=ct,
        defaults={
            "name": "Can view platform-wide revenue statistics",
        },
    )


def remove_view_platform_revenue_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    ct = ContentType.objects.get(app_label="quickstart", model="payment")
    Permission.objects.filter(
        codename="view_platform_revenue",
        content_type=ct,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0240_widgetsubscription_drop_duplicate_view_perm"),
    ]

    operations = [
        migrations.RunPython(
            add_view_platform_revenue_permission,
            remove_view_platform_revenue_permission,
        ),
    ]
