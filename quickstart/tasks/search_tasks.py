from backend.quickstart.models import BusinessInfo
from celery import shared_task


@shared_task
def update_search_vector_for_business(business_id):
    business = BusinessInfo.objects.get(pk=business_id)
    # Get all class IDs at once
    class_ids = list(business.classes.values_list("classId", flat=True))
    # In a real implementation, you'd trigger a more complex update,
    # but the key is to bulk-process or batch the updates.
    # For now, let's just log it as a placeholder.
    print(
        f"Queued search vector update for {len(class_ids)} classes of business {business_id}"
    )
    # A more advanced solution would use Django's bulk_update or raw SQL
    # to update all vectors in a single, efficient query.
