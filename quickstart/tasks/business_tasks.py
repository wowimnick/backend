from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Max, Q, Count
from django.template.loader import render_to_string
from django.conf import settings
from django.core.cache import cache
from collections import defaultdict
from urllib.parse import quote
import logging

from quickstart.models import BusinessInfo, ClassesMain, ClassCollection, BlogPost, BlogCategory
from quickstart.utils.services import CollectionAutoAssigner
from quickstart.utils.blog_ai_service import generate_blog_draft
import resend

logger = logging.getLogger(__name__)

@shared_task
def classify_class_task(class_id):
    """
    Background task to run automation rules on a single class.
    Triggered on class creation or update.
    """
    try:
        instance = ClassesMain.objects.get(pk=class_id)
        assigner = CollectionAutoAssigner()
        assigner.process_class(instance)
    except ClassesMain.DoesNotExist:
        logger.warning(f"Class {class_id} not found during classification task.")
    except Exception as e:
        logger.error(f"Error classifying class {class_id}: {e}", exc_info=True)

@shared_task
def update_trending_collections_task():
    """
    Scheduled task (e.g., nightly) to re-evaluate all active classes.
    Useful for time-based rules like 'Newness' or dynamic scores.
    """
    assigner = CollectionAutoAssigner()
    # Process in batches to avoid memory issues
    active_classes = ClassesMain.objects.filter(status='active').iterator()
    
    count = 0
    for cls in active_classes:
        assigner.process_class(cls)
        count += 1
    
    logger.info(f"Updated trending collections for {count} classes.")

@shared_task
def notify_businesses_of_expiring_schedules():
    """
    Checks for active classes that need new schedules.
    
    ZERO SPAM GUARANTEE:
    Locks each business ID in cache for 24 hours to prevent duplicate 
    emails if task is re-run or retried.
    """
    today = timezone.now().date()
    warning_threshold = today + timedelta(days=14)
    
    logger.info("Starting schedule expiry check task.")

    # 1. Query active classes
    classes_needing_schedules = ClassesMain.objects.filter(
        status='active',
        businessId__isActive=True,
        businessId__scheduleExpiryNotification=True
    ).annotate(
        last_scheduled_date=Max('options__schedules__instances__date')
    ).filter(
        Q(last_scheduled_date__lte=warning_threshold) | Q(last_scheduled_date__isnull=True)
    ).select_related('businessId', 'businessId__owner')

    if not classes_needing_schedules.exists():
        return "No classes found needing schedule updates."

    # 2. Group by Business
    business_map = defaultdict(list)
    for cls in classes_needing_schedules:
        business_map[cls.businessId].append({
            'title': cls.title,
            'last_date': cls.last_scheduled_date
        })

    # 3. Prepare Email Batch
    email_batch = []
    
    try:
        resend.api_key = settings.RESEND_API_KEY
    except AttributeError:
        return "Failed: Missing Resend API Key."

    for business, class_list in business_map.items():
        # ATOMIC LOCK: Lock this business for 24 hours
        cache_key = f"schedule_expiry_alert_{business.businessId}_{today}"
        
        if not cache.add(cache_key, True, timeout=86400):
            logger.info(f"Skipping business {business.businessId} (Email already queued today)")
            continue

        owner = business.owner
        if not owner or not owner.email:
            continue

        context = {
            'business_user': owner,
            'expiring_classes': class_list,
            'dashboard_url': f"{settings.FRONTEND_BASE_URL}/business/dashboard/overview",
            'settings_url': f"{settings.FRONTEND_BASE_URL}/business/dashboard",
            'settings': settings 
        }

        html_content = render_to_string("emails/business_schedule_expiry_warning.html", context)
        
        email_batch.append({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": [owner.email],
            "subject": f"Action Required: {len(class_list)} of your classes are ending soon",
            "html": html_content,
        })

    if not email_batch:
        return "No valid business emails found to send (or all locked)."

    # 4. Send in chunks
    BATCH_SIZE = 100
    sent_count = 0

    for i in range(0, len(email_batch), BATCH_SIZE):
        chunk = email_batch[i:i + BATCH_SIZE]
        try:
            resend.Batch.send(chunk)
            sent_count += len(chunk)
            logger.info(f"Successfully sent batch of {len(chunk)} schedule expiry emails.")
        except Exception as e:
            # Note: We do NOT release the locks. 
            # If the batch fails, we assume it might have partially sent. 
            # Better to miss an email than spam.
            logger.error(f"Failed to send batch (index {i}): {e}")

    return f"Processed schedule expiry. Sent {sent_count} emails."


def _get_blog_ai_default_category():
    """Get or create the default BlogCategory for AI-generated drafts."""
    slug = getattr(settings, "BLOG_AI_DEFAULT_CATEGORY_SLUG", "tips-and-guides")
    name = getattr(settings, "BLOG_AI_DEFAULT_CATEGORY_NAME", "Tips & Guides")
    category, _ = BlogCategory.objects.get_or_create(
        slug=slug,
        defaults={"name": name},
    )
    return category


def _pick_topic_for_weekly_blog():
    """
    Pick one collection and one location for this week's blog draft.
    Uses cache to rotate and avoid repeating the same combo within 7 days.
    Returns (collection, location_str) or (None, None) if no data.
    """
    collections = list(
        ClassCollection.objects.filter(is_active=True).order_by("sort_order")
    )
    if not collections:
        return None, None

    # Top locations by active class count (city, state)
    location_rows = (
        ClassesMain.objects.filter(
            status="active",
            businessId__isActive=True,
        )
        .exclude(businessId__businessCity__isnull=True)
        .exclude(businessId__businessCity="")
        .values("businessId__businessCity", "businessId__businessState")
        .annotate(count=Count("classId"))
        .order_by("-count")[:15]
    )
    locations = [
        (item["businessId__businessCity"], item["businessId__businessState"] or "")
        for item in location_rows
    ]
    if not locations:
        # No location filter: use collection only
        locations = [(None, None)]

    # Idempotency: avoid same collection+location within 7 days
    recent_key = "blog_ai_recent_combos"
    recent = cache.get(recent_key) or []
    cutoff = timezone.now() - timedelta(days=7)
    recent = [(c, loc, ts) for (c, loc, ts) in recent if ts > cutoff]
    cache.set(recent_key, recent, timeout=7 * 86400)
    recent_combos = {(c, loc) for (c, loc, ts) in recent}

    # Rotation: try each (collection, location) until we find one not in recent
    for coll in collections:
        for city, state in locations:
            if city:
                location_str = f"{city}, {state}" if state else city
            else:
                location_str = ""
            combo_key = (coll.slug, location_str)
            if combo_key in recent_combos:
                continue
            return coll, location_str

    # All combos used recently; pick first anyway and let duplicate slug be handled
    coll = collections[0]
    city, state = locations[0]
    location_str = f"{city}, {state}" if (city and state) else (city or "")
    return coll, location_str


@shared_task
def generate_weekly_blog_draft_task():
    """
    Generate one blog post draft per run using Gemini. Topic is derived from
    active collections and top locations. Saves as draft for human review.
    Run weekly via Celery Beat. Fails safely: any exception is caught, logged,
    and returned as a string so the task never raises (worker stays healthy).
    """
    try:
        if not getattr(settings, "BLOG_AI_ENABLED", True):
            logger.info("BLOG_AI_ENABLED is False; skipping weekly blog draft.")
            return "Skipped (BLOG_AI_ENABLED=False)."

        if not getattr(settings, "GEMINI_API_KEY", None):
            logger.warning("GEMINI_API_KEY missing; cannot generate blog draft.")
            return "Skipped (no GEMINI_API_KEY)."

        collection, location_str = _pick_topic_for_weekly_blog()
        if not collection:
            logger.info("No active collections; skipping weekly blog draft.")
            return "Skipped (no collections)."

        if location_str:
            topic_hint = f"Best {collection.name} experiences in {location_str}"
            explore_url = (
                f"https://classeasily.com/explore?collection={quote(collection.slug)}&location={quote(location_str)}"
            )
        else:
            topic_hint = f"Best {collection.name} experiences"
            explore_url = f"https://classeasily.com/explore?collection={quote(collection.slug)}"

        draft_data = generate_blog_draft(
            topic_hint=topic_hint,
            explore_url=explore_url,
            site_name="Classeasily",
            word_count_target=(400, 600),
        )
        if not draft_data:
            logger.warning("Gemini returned no blog draft; skipping.")
            return "Skipped (no draft from Gemini)."

        category = _get_blog_ai_default_category()
        default_image_url = getattr(
            settings,
            "BLOG_AI_DEFAULT_IMAGE_URL",
            "https://classeasily.com/images/blog-placeholder.jpg",
        )

        post = BlogPost(
            title=draft_data["title"],
            excerpt=(draft_data.get("excerpt") or "")[:500],
            content=draft_data.get("content") or "",
            slug=(draft_data.get("slug") or "").strip() or None,
            tags=draft_data.get("tags") or [],
            status="draft",
            author=None,
            category=category,
            image_url=default_image_url,
        )
        post.save()

        # Idempotency: mark this combo as used so we don't repeat within 7 days
        recent_key = "blog_ai_recent_combos"
        recent = cache.get(recent_key) or []
        recent.append((collection.slug, location_str, timezone.now()))
        cache.set(recent_key, recent[-50:], timeout=7 * 86400)

        logger.info(
            "Created weekly blog draft id=%s title=%s (collection=%s, location=%s)",
            post.id,
            post.title,
            collection.slug,
            location_str or "none",
        )
        return f"Created draft id={post.id} title={post.title!r}"
    except Exception as e:
        logger.exception("Weekly blog draft task failed: %s", e)
        return f"Failed (safe): {e!r}"