# In quickstart/management/commands/reprocess_originals.py

from django.core.management.base import BaseCommand
from django.conf import settings
import boto3
from PIL import Image
import io


class Command(BaseCommand):
    help = "Finds all .webp files in the originals/ directory and creates resized versions in public/."

    def handle(self, *args, **options):
        s3_client = boto3.client("s3")
        s3_resource = boto3.resource("s3")
        bucket_name = settings.AWS_STORAGE_BUCKET_NAME
        bucket = s3_resource.Bucket(bucket_name)

        self.stdout.write(
            self.style.SUCCESS(
                f"--- Starting reprocessing for bucket: {bucket_name} ---"
            )
        )

        # Sizes to generate, matching your Lambda
        output_sizes = {
            "thumb": (200, 200),
            "medium": (800, 800),
            "large": (1200, 1200),
        }

        # Use iterator to handle potentially many files
        objects_to_process = bucket.objects.filter(Prefix="originals/")
        processed_count = 0
        created_count = 0

        for obj in objects_to_process:
            key = obj.key
            processed_count += 1

            # We only want to process .webp files in the originals directory
            if not key.lower().endswith(".webp"):
                continue

            self.stdout.write(f"Processing: {key}")

            try:
                # 1. Get the original WebP image from S3
                response = s3_client.get_object(Bucket=bucket_name, Key=key)
                original_image_bytes = response["Body"].read()

                # 2. Resize and create new versions
                with Image.open(io.BytesIO(original_image_bytes)) as img:
                    for size_name, size_info in output_sizes.items():
                        # Create a copy to avoid modifying the original image object in memory
                        img_copy = img.copy()
                        img_copy.thumbnail(size_info, Image.Resampling.LANCZOS)

                        buffer = io.BytesIO()
                        img_copy.save(buffer, format="WEBP", quality=85)
                        buffer.seek(0)

                        # 3. Construct the new key for the resized image
                        # e.g., 'originals/avatars/my-avatar.webp' -> 'public/thumb/avatars/my-avatar.webp'
                        new_key = key.replace("originals/", f"public/{size_name}/", 1)

                        # 4. Upload the new resized image to the public path
                        s3_client.put_object(
                            Bucket=bucket_name,
                            Key=new_key,
                            Body=buffer,
                            ContentType="image/webp",
                        )
                        created_count += 1
                        self.stdout.write(
                            self.style.SUCCESS(f"  -> Created: {new_key}")
                        )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  -> FAILED to process {key}. Error: {e}")
                )

        self.stdout.write(self.style.SUCCESS(f"\n--- Reprocessing Complete ---"))
        self.stdout.write(
            f"Scanned {processed_count} objects. Created {created_count} resized images."
        )
