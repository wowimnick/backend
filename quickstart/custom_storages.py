# quickstart/custom_storages.py

from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class PrivateMediaStorage(S3Boto3Storage):
    """
    Custom storage class for private media files.
    This class ensures that all generated URLs are pre-signed URLs
    with a default expiration time.
    """

    location = ""  # Store files at the root of your S3 bucket
    default_acl = "private"  # Ensures all uploaded files are private
    file_overwrite = False  # Prevents accidentally overwriting files with the same name
    custom_domain = False  # We want the direct S3 URL for pre-signing

    # This is the key setting that controls pre-signed URLs
    querystring_auth = True
    querystring_expire = 3600  # URLs expire in 1 hour (3600 seconds)

    def url(self, name, parameters=None, expire=None, http_method=None):
        """
        Overrides the default .url() method to always return a pre-signed URL.
        This makes it work seamlessly with DRF's ImageField(use_url=True).
        """
        if expire is None:
            expire = self.querystring_expire

        try:
            return super().url(name, parameters, expire, http_method)
        except Exception as e:
            logger.error(f"Error generating pre-signed URL for {name}: {e}")
            # Return an empty string or a placeholder URL on failure
            return ""
