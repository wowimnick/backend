# Path: your_app/management/commands/link_verification_requests.py

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from ...models import BusinessInfo, VerificationRequest, AuditLog

User = get_user_model()

class Command(BaseCommand):
    help = 'Links existing businesses with pending verification status to proper VerificationRequest records'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run without making actual changes',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        
        # Get all businesses with pending verification but no corresponding VerificationRequest
        businesses_to_link = BusinessInfo.objects.filter(
            verificationStatus='pending'
        ).exclude(
            verification_requests__isnull=False
        )
        
        count = 0
        errors = []
        
        self.stdout.write(
            self.style.SUCCESS(f"Found {businesses_to_link.count()} businesses that need verification requests")
        )
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN - No changes will be made"))
        
        # Create verification requests for each business
        for business in businesses_to_link:
            try:
                if not business.owner:
                    errors.append(f"Business {business.businessName} (ID: {business.businessId}) has no owner")
                    continue
                
                if not dry_run:
                    with transaction.atomic():
                        # Create the verification request
                        verification_request = VerificationRequest.objects.create(
                            user=business.owner,
                            business=business,
                            status='pending',
                            submitted_at=business.createdAt or timezone.now(),
                            notes=f"Auto-generated verification request for existing business"
                        )
                        
                        # Log the creation for audit trail
                        AuditLog.objects.create(
                            user=business.owner,
                            user_email=business.owner.email,
                            action='verification_submit',
                            details=f"Verification request auto-generated for existing business",
                            target_model='VerificationRequest',
                            target_id=str(verification_request.id),
                            metadata={
                                'business_id': business.businessId,
                                'business_name': business.businessName,
                                'auto_generated': True
                            }
                        )
                
                count += 1
                self.stdout.write(f"{'Would create' if dry_run else 'Created'} verification request for: {business.businessName}")
            
            except Exception as e:
                errors.append(f"Error processing business {business.businessName} (ID: {business.businessId}): {str(e)}")
        
        # Print summary
        self.stdout.write("\nSummary:")
        self.stdout.write(f"- Total businesses processed: {businesses_to_link.count()}")
        self.stdout.write(f"- Verification requests {'would be' if dry_run else ''} created: {count}")
        self.stdout.write(f"- Errors encountered: {len(errors)}")
        
        if errors:
            self.stdout.write("\nErrors:")
            for error in errors:
                self.stdout.write(self.style.ERROR(f"- {error}"))
        
        if not dry_run and count > 0:
            self.stdout.write(self.style.SUCCESS(f"\nSuccessfully created {count} verification requests"))