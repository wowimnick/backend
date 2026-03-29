"""Lightweight HTML cleanup for marketing content (no new dependencies)."""
import re


def sanitize_marketing_html(html: str) -> str:
    if not html or not isinstance(html, str):
        return ""
    # Strip script and style blocks
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", html)
    # Remove javascript: URLs in anchors
    html = re.sub(r"(?i)\sjavascript\s*:", "", html)
    return html[:500_000]
