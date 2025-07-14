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
