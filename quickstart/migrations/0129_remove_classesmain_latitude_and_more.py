from django.db import migrations
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.indexes import GistIndex
# Add this import for creating Point objects
from django.contrib.gis.geos import Point

# --- Our custom data migration function ---
def populate_point_from_coordinates(apps, schema_editor):
    ClassesMain = apps.get_model("quickstart", "ClassesMain")
    # Use .iterator() to process in chunks and save memory
    for class_instance in ClassesMain.objects.all().iterator():
        # Check if the coordinates string is valid and not empty
        if class_instance.coordinates and isinstance(class_instance.coordinates, str):
            try:
                # The format is "lat,lng"
                parts = class_instance.coordinates.split(',')
                if len(parts) == 2:
                    lat_str, lng_str = parts[0].strip(), parts[1].strip()
                    lat, lng = float(lat_str), float(lng_str)

                    # IMPORTANT: The Point constructor is (x, y), which means (longitude, latitude).
                    class_instance.point = Point(lng, lat, srid=4326)
                    class_instance.save(update_fields=['point'])

            except (ValueError, TypeError, IndexError):
                # This will catch errors from float() conversion or if the split fails.
                # We simply skip the problematic row and continue the migration.
                print(f"Skipping malformed coordinate string for Class ID {class_instance.pk}: '{class_instance.coordinates}'")
                pass

# A function to run if we ever need to reverse this migration (good practice)
def revert_point_to_null(apps, schema_editor):
    ClassesMain = apps.get_model("quickstart", "ClassesMain")
    ClassesMain.objects.all().update(point=None)

class Migration(migrations.Migration):

    dependencies = [
        ('quickstart', '0128_alter_payout_arrival_date'),
    ]

    operations = [
        # 1. ADD the new 'point' field first. It will be created as an empty column.
        migrations.AddField(
            model_name='classesmain',
            name='point',
            field=gis_models.PointField(blank=True, help_text='Geographic location stored in PostGIS for spatial queries.', null=True, srid=4326),
        ),

        # 2. RUN our Python code to parse the 'coordinates' string and populate the new 'point' field.
        migrations.RunPython(populate_point_from_coordinates, reverse_code=revert_point_to_null),

        # 3. REMOVE the old, obsolete latitude and longitude columns.
        migrations.RemoveField(
            model_name='classesmain',
            name='latitude',
        ),
        migrations.RemoveField(
            model_name='classesmain',
            name='longitude',
        ),

        # 4. ADD the spatial index on the new, now-populated 'point' field for performance.
        migrations.AddIndex(
            model_name='classesmain',
            index=GistIndex(fields=['point'], name='classes_point_gist_idx'), # Use a custom name
        ),
    ]