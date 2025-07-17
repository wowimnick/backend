from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings
import logging
import io
from PIL import Image
from django.core.files.base import ContentFile
import os

logger = logging.getLogger(__name__)


class PrivateMediaStorage(S3Boto3Storage):
    """
    Custom storage class for private media files.
    This class ensures that all generated URLs are pre-signed URLs
    with a default expiration time.
    """

    location = ""
    default_acl = "private"
    file_overwrite = False
    custom_domain = False  # Important: MUST be False for pre-signing to work

    querystring_auth = True
    querystring_expire = 3600  # URLs expire in 1 hour (3600 seconds)

    def url(self, name, parameters=None, expire=None, http_method=None):
        """
        Overrides the default .url() method to always return a pre-signed URL.
        """
        if expire is None:
            expire = self.querystring_expire
        try:
            return super().url(name, parameters, expire, http_method)
        except Exception as e:
            logger.error(f"Error generating pre-signed URL for {name}: {e}")
            return ""


class PublicMediaStorage(S3Boto3Storage):
    """
    Custom storage class for public media files served via CloudFront.
    This generates a clean URL pointing to your CloudFront distribution.
    """

    # The Lambda function places resized files in the 'public/' directory.
    # Setting the location here is a good default.
    location = "public"

    # Files can remain private in S3 because CloudFront uses Origin Access Control.
    default_acl = "private"
    file_overwrite = False

    # This is the most important setting. It tells Django to use your
    # CloudFront domain. You MUST define CLOUDFRONT_DOMAIN in your settings.py.
    custom_domain = getattr(settings, "CLOUDFRONT_DOMAIN", None)

    # We do NOT want pre-signed URLs for public, cached assets.
    querystring_auth = False


class WebPStorage(S3Boto3Storage):
    """
    Custom storage class that converts all uploaded images to WebP format
    before saving them to Amazon S3.
    """

    def _save(self, name, content):
        """
        Overrides the default save method to perform the WebP conversion.
        """
        # The 'content' is a Django File object. We need to read its bytes.
        image_bytes = content.read()

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                # Pillow might not be able to process some file types.
                # If it's not a valid image, we can't convert it.

                buffer = io.BytesIO()
                # Use a high quality setting. 85 is a great balance.
                # This will also preserve transparency from formats like PNG.
                img.save(buffer, format="WEBP", quality=85)

                # The buffer now contains the WebP image bytes.
                # We need to wrap it in a ContentFile to make it a Django-compatible file.
                webp_content = ContentFile(buffer.getvalue())

        except Exception as e:
            # If Pillow can't open it, it's probably not an image (e.g., SVG, corrupt file).
            # In this case, we'll save the original content without conversion.
            logger.error(
                f"Could not convert {name} to WebP, saving original. Error: {e}"
            )
            # Reset the original content's read pointer before saving
            content.seek(0)
            webp_content = content  # Fallback to original

        # --- Change the filename's extension to .webp ---
        base_name, _ = os.path.splitext(name)
        webp_name = base_name + ".webp"

        # Now, call the parent S3Boto3Storage's _save method with the
        # NEW filename and the NEW (WebP) content.
        return super()._save(webp_name, webp_content)
