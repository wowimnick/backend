import hashlib
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
from quickstart.views.public.public_class_views import (
    HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY,
    invalidate_public_class_search_preset_cache,
)
from quickstart.utils.revalidation import trigger_nextjs_revalidation
from quickstart.utils.experience_theme_coverage import (
    ensure_preset_collection_memberships,
)
from quickstart.utils.description_formatter import DescriptionFormatter
from quickstart.utils.description_ai import description_ai_has_output
from quickstart.utils.blog_ai_service import generate_blog_draft
import resend

logger = logging.getLogger(__name__)


def _revalidate_class_detail_tags(class_instance) -> None:
    """Revalidate Next.js class-detail cache tags (same tags as business class saves)."""
    if not class_instance:
        return
    try:
        slug = getattr(class_instance, "slug", None)
        if slug:
            trigger_nextjs_revalidation(tag=f"class-{slug}")
        trigger_nextjs_revalidation(tag=f"class-{class_instance.classId}")
        trigger_nextjs_revalidation(tag="classes")
    except Exception as e:
        logger.warning("Post-Gemini class detail revalidation failed: %s", e)


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
        _revalidate_class_detail_tags(instance)
    except ClassesMain.DoesNotExist:
        logger.warning(f"Class {class_id} not found during classification task.")
    except Exception as e:
        logger.error(f"Error classifying class {class_id}: {e}", exc_info=True)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def format_class_description_task(self, class_id, force=False):
    """
    Background task: Gemini formats description into summary + collapsible sections.

    When ``force`` is True, always calls Gemini (unless description is empty), even if
    status is already ``ready`` and the source hash is unchanged.
    """
    try:
        instance = ClassesMain.objects.get(pk=class_id)
    except ClassesMain.DoesNotExist:
        logger.warning("format_class_description_task: class %s not found", class_id)
        return

    raw = (instance.description or "").strip()
    logger.info(
        "format_class_description_task START class_id=%s force=%s slug=%r desc_chars=%s ai_status=%s",
        class_id,
        force,
        getattr(instance, "slug", None),
        len(raw),
        getattr(instance, "description_ai_status", None),
    )
    if not raw:
        logger.info(
            "format_class_description_task class_id=%s empty description; clearing AI fields",
            class_id,
        )
        ClassesMain.objects.filter(pk=class_id).update(
            description_summary="",
            description_sections=[],
            description_ai_source_hash="",
            description_ai_status="ready",
            description_ai_generated_at=timezone.now(),
        )
        _revalidate_class_detail_tags(instance)
        return

    new_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    has_output = description_ai_has_output(instance)
    if (
        not force
        and instance.description_ai_status == "ready"
        and instance.description_ai_source_hash == new_hash
        and has_output
    ):
        logger.info(
            "format_class_description_task SKIP class_id=%s unchanged hash matches ready",
            class_id,
        )
        return

    if (
        not force
        and instance.description_ai_source_hash == new_hash
        and has_output
        and instance.description_ai_status != "ready"
    ):
        logger.info(
            "format_class_description_task class_id=%s normalizing ai_status=%s -> ready (hash+output match)",
            class_id,
            instance.description_ai_status,
        )
        ClassesMain.objects.filter(pk=class_id).update(
            description_ai_status="ready",
            description_ai_generated_at=timezone.now(),
        )
        _revalidate_class_detail_tags(instance)
        return

    try:
        DescriptionFormatter().process(instance, new_hash)
        instance.refresh_from_db(
            fields=[
                "description_summary",
                "description_sections",
                "description_ai_status",
                "description_ai_source_hash",
            ]
        )
        logger.info(
            "format_class_description_task DONE class_id=%s slug=%r status=%s summary_chars=%s sections_n=%s",
            class_id,
            getattr(instance, "slug", None),
            instance.description_ai_status,
            len(instance.description_summary or ""),
            len(instance.description_sections or []),
        )
        if instance.description_ai_status == "ready":
            _revalidate_class_detail_tags(instance)
    except Exception as e:
        logger.exception(
            "format_class_description_task failed for class_id=%s", class_id
        )
        retries = getattr(self.request, "retries", 0) or 0
        max_retries = getattr(self, "max_retries", 3) or 3
        if retries >= max_retries:
            ClassesMain.objects.filter(pk=class_id).update(
                description_ai_status="failed"
            )
            return
        raise self.retry(exc=e)


@shared_task
def reconcile_stuck_description_ai_task():
    """
    Re-queue active classes stuck on ``stale`` or ``pending`` with no summary/sections.

    - ``stale``: never processed (e.g. predates the feature, or inactive at creation)
    - ``pending``: task was queued but worker never finished (crash / lost message)
    """
    from quickstart.utils.description_ai import description_ai_stuck_queryset

    stuck_ids = list(
        description_ai_stuck_queryset()
        .order_by("updatedAt")
        .values_list("classId", flat=True)[:100]
    )
    if not stuck_ids:
        return "ok (0 stuck)"

    ClassesMain.objects.filter(classId__in=stuck_ids).update(
        description_ai_status="pending"
    )
    for pk in stuck_ids:
        format_class_description_task.delay(pk, force=True)

    logger.warning(
        "reconcile_stuck_description_ai_task re-queued %s class(es): %s",
        len(stuck_ids),
        stuck_ids[:20],
    )
    return f"requeued={len(stuck_ids)}"


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


@shared_task(name="quickstart.tasks.business_tasks.reclassify_automated_collection_task")
def reclassify_automated_collection_task(collection_id: int):
    """
    Re-evaluate membership for a single automated collection (Gemini per active class).
    Run only when an admin explicitly triggers reclassify — not on collection save.
    """
    col = (
        ClassCollection.objects.filter(pk=collection_id)
        .only("pk", "name", "type", "automation_rules", "slug")
        .first()
    )
    if not col:
        logger.warning("reclassify_automated_collection_task: collection id=%s not found", collection_id)
        return {"ok": False, "reason": "not_found"}
    if col.type != "automated":
        logger.info("reclassify_automated_collection_task: id=%s is not automated, skipping", collection_id)
        return {"ok": False, "reason": "not_automated"}
    raw_ar = col.automation_rules if isinstance(col.automation_rules, dict) else {}
    criteria = (raw_ar.get("ai_criteria") or "").strip() if isinstance(raw_ar, dict) else ""
    if not criteria:
        logger.warning(
            "reclassify_automated_collection_task: collection id=%s has no ai_criteria, skipping",
            collection_id,
        )
        return {"ok": False, "reason": "no_ai_criteria"}

    collections_info = [{"id": col.pk, "name": col.name, "criteria": criteria}]
    assigner = CollectionAutoAssigner()

    scanned = 0
    added = 0
    removed = 0
    for cls_obj in ClassesMain.objects.filter(status="active").iterator():
        scanned += 1
        matched_ids = assigner._call_llm_curator(cls_obj, collections_info)
        want = col.pk in (matched_ids or [])
        has = cls_obj.collections.filter(pk=col.pk).exists()
        if want and not has:
            cls_obj.collections.add(col)
            added += 1
        elif not want and has:
            cls_obj.collections.remove(col)
            removed += 1

    try:
        cache.delete(HOMEPAGE_CONTENT_COLLECTIONS_CACHE_KEY)
        invalidate_public_class_search_preset_cache(
            affected_collection_slugs=[col.slug] if getattr(col, "slug", None) else None
        )
    except Exception as e:
        logger.warning("Post-reclassify cache invalidation failed: %s", e)

    try:
        trigger_nextjs_revalidation(tag="homepage-content")
        trigger_nextjs_revalidation(tag="collections")
        trigger_nextjs_revalidation(tag="homepage-classes")
        trigger_nextjs_revalidation(tag="classes-search")
    except Exception as e:
        logger.warning("Post-reclassify Next.js revalidation trigger failed: %s", e)

    logger.info(
        "reclassify_automated_collection_task done collection_id=%s scanned=%s added=%s removed=%s",
        collection_id,
        scanned,
        added,
        removed,
    )
    return {
        "ok": True,
        "collection_id": collection_id,
        "scanned": scanned,
        "added": added,
        "removed": removed,
    }


@shared_task
def ensure_preset_experience_theme_coverage_task(thematic_collection_ids: list[int]):
    """
    Chained after ``update_trending_collections_task`` when bulk-loading the curated
    "experience themes" preset: link any active classes that still belong to zero of
    those collections (keyword title/description match first, otherwise default).
    """
    try:
        return ensure_preset_collection_memberships(thematic_collection_ids)
    except Exception as e:
        logger.exception("ensure_preset_experience_theme_coverage_task failed: %s", e)


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
            site_name="ClassEasily",
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


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def send_business_announcement_task(
    self,
    owner_emails,
    title,
    message,
    send_email=True,
    send_in_app=True,
    urgency="normal",
    admin_email=None,
):
    """Send platform announcement emails and/or in-app notifications to business owners."""
    from django.contrib.auth import get_user_model

    from quickstart.utils.notification_utils import create_notifications_for_users
    from quickstart.tasks.email_tasks import send_transactional_email_task

    User = get_user_model()
    emails = [e for e in (owner_emails or []) if e]
    if not emails:
        return "No recipients"

    users = list(User.objects.filter(email__in=emails, is_active=True))
    sent_count = 0

    if send_in_app and users:
        urgency_prefix = "[URGENT] " if urgency == "urgent" else ""
        create_notifications_for_users(
            users,
            "system_announcement",
            f"{urgency_prefix}{title}: {message}",
            "Megaphone",
            "#ff385c",
            "/business/dashboard",
        )

    if send_email:
        html_body = (
            f"<h2>{title}</h2><p>{message}</p>"
            f"<p style='color:#64748b;font-size:12px;'>Sent by ClassEasily platform administration.</p>"
        )
        for email in emails:
            try:
                send_transactional_email_task.delay(
                    to=email,
                    subject=title,
                    html=html_body,
                )
                sent_count += 1
            except Exception as exc:
                logger.error("Failed to queue announcement email to %s: %s", email, exc)

    logger.info(
        "Business announcement '%s' processed by %s: %s emails queued, %s in-app users",
        title,
        admin_email or "system",
        sent_count,
        len(users) if send_in_app else 0,
    )
    return f"Queued {sent_count} emails, {len(users)} in-app notifications"


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def moderate_message_task(self, message_id):
    """
    Async scam moderation for quarantined booker messages.
    Fail-open on Gemini errors so genuine leads are never lost.
    """
    from django.utils import timezone

    from quickstart.models import ConversationMessage
    from quickstart.utils.conversation_delivery import deliver_booker_message
    from quickstart.utils.scam_filter_ai import classify_message

    try:
        msg = (
            ConversationMessage.objects.select_related(
                "conversation",
                "conversation__business",
                "sender_user",
                "sender_contact",
            )
            .get(pk=message_id)
        )
    except ConversationMessage.DoesNotExist:
        logger.warning("moderate_message_task: message %s not found", message_id)
        return

    if msg.moderation_status != ConversationMessage.MODERATION_PENDING:
        return

    conv = msg.conversation
    business = conv.business
    sender_name = "Guest"
    sender_email = None
    if msg.sender_user:
        sender_name = msg.sender_user.get_full_name() or msg.sender_user.email or sender_name
        sender_email = msg.sender_user.email
    elif msg.sender_contact:
        sender_name = (
            f"{msg.sender_contact.first_name} {msg.sender_contact.last_name}".strip()
            or msg.sender_contact.email
            or sender_name
        )
        sender_email = msg.sender_contact.email

    now = timezone.now()
    update_fields = ["moderation_status", "moderation_reason", "moderation_confidence", "moderated_at"]

    try:
        result = classify_message(
            msg.text,
            business_name=getattr(business, "businessName", None),
            sender_name=sender_name,
            sender_email=sender_email,
        )
        if result.get("is_scam"):
            msg.moderation_status = ConversationMessage.MODERATION_REJECTED
            msg.moderation_reason = (result.get("reason") or "")[:2000]
            msg.moderation_confidence = result.get("confidence")
            msg.moderated_at = now
            msg.save(update_fields=update_fields)
            logger.info(
                "Message %s rejected by scam filter (confidence=%s)",
                message_id,
                result.get("confidence"),
            )
            return

        msg.moderation_status = ConversationMessage.MODERATION_APPROVED
        msg.moderation_reason = (result.get("reason") or "")[:2000]
        msg.moderation_confidence = result.get("confidence")
        msg.moderated_at = now
        msg.save(update_fields=update_fields)
        deliver_booker_message(conv, msg)
    except Exception as exc:
        logger.error(
            "moderate_message_task failed for %s; fail-open deliver: %s",
            message_id,
            exc,
            exc_info=True,
        )
        msg.moderation_status = ConversationMessage.MODERATION_ERROR
        msg.moderation_reason = str(exc)[:2000]
        msg.moderated_at = now
        msg.save(update_fields=update_fields)
        deliver_booker_message(conv, msg)