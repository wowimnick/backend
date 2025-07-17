# In quickstart/management/commands/convert_images_to_webp.py

from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from PIL import Image
import io
import os

# Import all the models with ImageFields
from quickstart.models import (
    CustomUser,
    BusinessInfo,
    ClassImage,
    ClassCategory,
    Reviews,
)


class Command(BaseCommand):
    help = "Converts existing ImageField files to WebP format and moves them to their new upload_to location."

    def handle_model(self, model_class, field_name):
        self.stdout.write(f"--- Processing model: {model_class.__name__} ---")
        # Use iterator() to handle potentially large querysets efficiently
        queryset = model_class.objects.iterator()

        # We cannot get a total count with iterator, so we'll just log progress
        processed_count = 0
        converted_count = 0

        for instance in queryset:
            processed_count += 1
            if processed_count % 100 == 0:
                self.stdout.write(f"  ...processed {processed_count} records...")

            image_field = getattr(instance, field_name)

            if not image_field:
                continue  # Skip if image is blank

            original_path = image_field.name
            if original_path.lower().endswith(".webp"):
                continue  # Already converted

            self.stdout.write(f"  Converting {original_path}...")

            try:
                # Read original image from its current location in storage
                with image_field.open("rb") as f:
                    image_bytes = f.read()

                # Convert to WebP in memory
                with Image.open(io.BytesIO(image_bytes)) as img:
                    buffer = io.BytesIO()
                    # The `save` method will handle converting RGB/RGBA modes correctly
                    img.save(buffer, format="WEBP", quality=85)
                    webp_content = ContentFile(buffer.getvalue())

                # --- THIS IS THE CRITICAL CHANGE ---
                # Get just the original filename (e.g., 'my-photo.png')
                original_filename = os.path.basename(original_path)
                # Get the base filename without extension (e.g., 'my-photo')
                base_filename, _ = os.path.splitext(original_filename)
                # Create the new filename (e.g., 'my-photo.webp')
                webp_filename = base_filename + ".webp"

                # Now, save the file using ONLY its new name.
                # The storage backend will automatically use the field's NEW `upload_to`
                # setting to place it in the correct directory (e.g., 'originals/avatars/').
                # `save=False` prevents an immediate database save.
                image_field.save(webp_filename, webp_content, save=False)

                # Now save the instance to update the database path for this one field.
                instance.save(update_fields=[field_name])

                # IMPORTANT: Delete the OLD file from storage using its original path.
                image_field.storage.delete(original_path)

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  -> Successfully converted and moved. New path: {image_field.name}"
                    )
                )
                converted_count += 1

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f"  -> FAILED to convert {original_path}. Error: {e}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished {model_class.__name__}. Converted {converted_count} images.\n"
            )
        )

    def handle(self, *args, **options):
        models_and_fields = [
            (CustomUser, "avatar"),
            (BusinessInfo, "businessImage"),
            (ClassImage, "image"),
            (ClassCategory, "image"),
            (Reviews, "image"),
        ]

        for model, field in models_and_fields:
            self.handle_model(model, field)

        self.stdout.write(
            self.style.SUCCESS("All image conversion and migration tasks complete.")
        )
