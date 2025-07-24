import uuid
from django.db import migrations, transaction
from django.utils.text import slugify


def generate_unique_slug(apps, schema_editor):
    """
    Generates a unique SEO-friendly slug for each existing ClassesMain instance.
    """
    ClassesMain = apps.get_model("quickstart", "ClassesMain")

    # Use a transaction to ensure this operation is atomic
    with transaction.atomic():
        # Get all classes that do not have a slug yet
        for klass in ClassesMain.objects.filter(slug__isnull=True).iterator():
            # We need to fetch the related business object to get the city
            # Note: In migrations, you must access related objects explicitly.
            # The .businessId syntax might not work directly, so we use .businessId_id
            BusinessInfo = apps.get_model("quickstart", "BusinessInfo")
            try:
                business = BusinessInfo.objects.get(pk=klass.businessId_id)
                business_city = business.businessCity
            except BusinessInfo.DoesNotExist:
                # Fallback if business somehow doesn't exist
                business_city = "location"

            # Create a base slug from city and title
            base_slug = slugify(f"{business_city} {klass.title}")

            if not base_slug:
                base_slug = "class"  # Fallback for empty titles/cities

            slug = base_slug
            # Ensure the generated slug is unique
            while ClassesMain.objects.filter(slug=slug).exists():
                random_suffix = uuid.uuid4().hex[:6]
                slug = f"{base_slug}-{random_suffix}"

            klass.slug = slug
            klass.save(update_fields=["slug"])


class Migration(migrations.Migration):

    dependencies = [
        ("quickstart", "0121_classesmain_slug_classesmain_classes_slug_a29860_idx"),
    ]

    operations = [
        migrations.RunPython(
            generate_unique_slug, reverse_code=migrations.RunPython.noop
        ),
    ]
