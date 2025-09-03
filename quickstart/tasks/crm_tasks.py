import pandas as pd
from celery import shared_task
from django.core.files.storage import default_storage
from quickstart.models import BusinessInfo, Contact, CustomUser
import logging

logger = logging.getLogger(__name__)


def sanitize_cell(value):
    """
    Sanitizes a value from a CSV/Excel file to prevent formula injection.
    Strips leading characters that could trigger formula execution in spreadsheets.
    """
    if value and isinstance(value, str):
        temp_value = value.lstrip()
        if temp_value.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


@shared_task
def process_contact_import(file_path, column_mapping, business_id):
    """
    Processes an uploaded student/contact file by streaming it from storage,
    processing in chunks, sanitizing input, and creating new Contact records.
    """
    try:
        business = BusinessInfo.objects.get(businessId=business_id)

        # --- FIX: Open a stream to the file from storage (S3, etc.) ---
        with default_storage.open(file_path, "rb") as file_stream:
            is_excel = file_path.endswith((".xlsx", ".xls"))

            existing_contact_emails = set(
                Contact.objects.filter(
                    business=business, email__isnull=False
                ).values_list("email", flat=True)
            )

            total_processed = 0
            created_count = 0
            skipped_count = 0

            reverse_mapping = {v: k for k, v in column_mapping.items()}
            email_col = reverse_mapping.get("email")
            fname_col = reverse_mapping.get("first_name")
            lname_col = reverse_mapping.get("last_name")
            phone_col = reverse_mapping.get("phone_number")

            # Use the file stream directly with pandas for chunked processing
            reader = (
                pd.read_excel(file_stream, chunksize=500, engine="openpyxl")
                if is_excel
                else pd.read_csv(file_stream, chunksize=500)
            )

            for chunk_df in reader:
                chunk_df.dropna(how="all", inplace=True)
                chunk_df = chunk_df.astype(str).replace("nan", "")

                contacts_to_create = []

                for index, row in chunk_df.iterrows():
                    total_processed += 1
                    email = row.get(email_col, "").lower().strip() if email_col else ""
                    first_name = sanitize_cell(row.get(fname_col, ""))

                    if not first_name and not email:
                        skipped_count += 1
                        continue

                    if email and email in existing_contact_emails:
                        skipped_count += 1
                        continue

                    new_contact = Contact(
                        business=business,
                        email=email if email else None,
                        first_name=first_name,
                        last_name=sanitize_cell(row.get(lname_col, "")),
                        phone_number=sanitize_cell(row.get(phone_col, "")),
                        source="import",
                    )

                    contacts_to_create.append(new_contact)
                    if email:
                        existing_contact_emails.add(email)

                if contacts_to_create:
                    created_batch = Contact.objects.bulk_create(
                        contacts_to_create, ignore_conflicts=True
                    )
                    created_count += len(created_batch)

        # Link contacts after creation
        unlinked_contacts = Contact.objects.filter(
            business=business, source="import", user__isnull=True, email__isnull=False
        )
        users_to_match = CustomUser.objects.filter(
            email__in=unlinked_contacts.values_list("email", flat=True)
        )
        email_to_user_map = {user.email: user for user in users_to_match}

        for contact in unlinked_contacts:
            if contact.email in email_to_user_map:
                contact.user = email_to_user_map[contact.email]
                contact.save(update_fields=["user"])

        logger.info(
            f"Import for Business {business_id} completed. "
            f"Total Rows: {total_processed}, Created: {created_count}, Skipped: {skipped_count}"
        )
        # TODO: Send a success notification to the user.

        return {
            "status": "success",
            "total": total_processed,
            "created": created_count,
            "skipped": skipped_count,
        }

    except BusinessInfo.DoesNotExist:
        logger.error(f"Import Task Failed: Business with ID {business_id} not found.")
        return {"status": "error", "message": "Business not found."}
    except Exception as e:
        logger.error(
            f"Unhandled exception in import task for Business {business_id}: {e}",
            exc_info=True,
        )
        # TODO: Send a failure notification to the user.
        return {
            "status": "error",
            "message": "An unexpected error occurred during processing.",
        }
    finally:
        # --- HOUSEKEEPING: Delete the temporary file from storage ---
        if default_storage.exists(file_path):
            default_storage.delete(file_path)
            logger.info(f"Cleaned up temporary import file: {file_path}")
