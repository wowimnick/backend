"""
URL utilities for CloudFront/S3 integration.

Ensures paths are properly encoded for URLs so that special characters
(e.g. + in filenames) resolve correctly. Browsers interpret + as space
in URLs; S3 expects + to be encoded as %2B.
"""

from urllib.parse import quote

from django.conf import settings


def build_cloudfront_url(path):
    """
    Build a CloudFront URL with a properly encoded path.

    Encodes special characters so S3/CloudFront can resolve the object:
    - + becomes %2B (otherwise interpreted as space)
    - ( ) and other RFC 3986 reserved chars are encoded
    - / is preserved as path separator (safe='/')

    Args:
        path: The S3 object key/path (e.g. public/medium/Image+Name.webp)

    Returns:
        Full CloudFront URL, or None if domain/path not configured.
    """
    if not path:
        return None
    domain = getattr(settings, "CLOUDFRONT_DOMAIN", None)
    if not domain:
        return None
    # Strip leading slash if present (we add it explicitly)
    path = path.lstrip("/")
    encoded_path = quote(path, safe="/")
    return f"{domain}/{encoded_path}"


def sanitize_filename_for_s3(filename):
    """
    Sanitize a filename before S3 upload to prevent URL encoding issues.

    Replaces spaces and + with hyphens. Use this when building S3 keys from
    user-provided filenames to avoid Access Denied (403) when CloudFront
    receives URLs where + is interpreted as space.

    Args:
        filename: The original filename (e.g. "Image+Name.webp")

    Returns:
        Sanitized filename (e.g. "Image-Name.webp")
    """
    if not filename:
        return filename
    return filename.replace("+", "-").replace(" ", "-")
