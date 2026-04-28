"""
AI-generated blog draft service using Google Gemini.
Used by the weekly blog draft Celery task to generate one draft per run.
"""
import json
import logging
from django.conf import settings

from google import genai
from google.genai import types
from quickstart.utils.gemini_retry import generate_content_with_retry

logger = logging.getLogger(__name__)


def generate_blog_draft(
    topic_hint,
    explore_url,
    site_name="ClassEasily",
    word_count_target=(400, 600),
):
    """
    Call Gemini to generate a single blog post draft as JSON.

    Args:
        topic_hint: Short theme string, e.g. "Best pottery experiences in Toronto".
        explore_url: Full URL to link in the post, e.g. https://classeasily.com/explore?collection=pottery&location=Toronto
        site_name: Brand name for the prompt.
        word_count_target: (min, max) words for the body.

    Returns:
        Dict with keys: title, excerpt, content (HTML), slug, tags (list).
        None on API or parse failure.
    """
    min_words, max_words = word_count_target
    try:
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")
        if not api_key:
            logger.error("GEMINI_API_KEY is missing; cannot generate blog draft.")
            return None

        client = genai.Client(api_key=api_key)

        prompt = f"""
You are a content writer for {site_name}, a platform where people discover and book local experiences and classes.

Write a short, friendly, SEO-aware blog post on this topic: "{topic_hint}"

Requirements:
- Target audience: people looking for local experiences, date ideas, or activities to do with friends/family.
- Length: between {min_words} and {max_words} words for the main body.
- Tone: helpful, inviting, and specific to the topic and location. No generic fluff.
- Include exactly one internal link in the body: "{explore_url}" with anchor text that fits the sentence (e.g. "browse pottery experiences in Toronto" or "see what's available here").
- Content must be valid HTML: use <p> for paragraphs, no markdown. You may use <strong> or <em> sparingly.

Output a single JSON object only. No markdown code fences. Use this exact structure:
{{
  "title": "Catchy, SEO-friendly title under 70 chars",
  "excerpt": "One or two sentences summarizing the post, under 160 chars.",
  "content": "<p>First paragraph...</p><p>Second paragraph with a <a href=\\"{explore_url}\\">relevant link</a>...</p><p>...</p>",
  "slug": "url-safe-slug-from-title",
  "tags": ["Tag1", "Tag2", "Tag3"]
}}
"""

        response = generate_content_with_retry(
            client,
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )

        if not response.text:
            logger.warning("Gemini returned empty response for blog draft.")
            return None

        data = json.loads(response.text)
        required = ("title", "excerpt", "content", "slug", "tags")
        if not all(k in data for k in required):
            logger.warning("Gemini blog response missing required keys: %s", list(data.keys()))
            return None

        if not isinstance(data.get("tags"), list):
            data["tags"] = []

        return data

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse Gemini blog JSON: %s", e)
        return None
    except Exception as e:
        logger.error("Gemini blog draft API failed: %s", e, exc_info=True)
        return None
