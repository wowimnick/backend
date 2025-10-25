import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from quickstart.models import ImportedGoogleReview
import os

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Debug Google review images - check database paths and expected S3 paths"

    def add_arguments(self, parser):
        parser.add_argument(
            '--business_id',
            type=int,
            required=True,
            help='The ID of the business to check reviews for',
        )
        parser.add_argument(
            '--check-s3',
            action='store_true',
            help='Also check if files exist in S3 (requires AWS credentials and boto3)',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=10,
            help='Number of reviews to check (default: 10)',
        )

    def handle(self, *args, **options):
        business_id = options['business_id']
        check_s3 = options.get('check_s3', False)
        limit = options.get('limit', 10)
        
        s3_client = None
        bucket_name = None
        
        if check_s3:
            try:
                import boto3
                from botocore.exceptions import ClientError
                
                s3_client = boto3.client('s3')
                bucket_name = getattr(settings, 'AWS_STORAGE_BUCKET_NAME', None)
                
                if not bucket_name:
                    self.stdout.write(
                        self.style.WARNING(
                            "⚠️  AWS_STORAGE_BUCKET_NAME not configured in settings"
                        )
                    )
                    check_s3 = False
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f"✓ Will check S3 bucket: {bucket_name}")
                    )
            except ImportError:
                self.stdout.write(
                    self.style.WARNING(
                        "⚠️  boto3 not installed. Install with: pip install boto3"
                    )
                )
                check_s3 = False
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f"⚠️  Could not initialize S3 client: {e}")
                )
                check_s3 = False

        # Get reviews
        reviews = ImportedGoogleReview.objects.filter(business_id=business_id)
        
        self.stdout.write(f"\n{'='*100}")
        self.stdout.write(f"🔍 DEBUGGING IMPORTED GOOGLE REVIEWS")
        self.stdout.write(f"{'='*100}")
        self.stdout.write(f"Business ID: {business_id}")
        self.stdout.write(f"Total reviews found: {reviews.count()}")
        self.stdout.write(f"Checking first {limit} reviews...")
        self.stdout.write(f"CloudFront domain: {settings.CLOUDFRONT_DOMAIN}")
        self.stdout.write(f"{'='*100}\n")

        if reviews.count() == 0:
            self.stdout.write(
                self.style.WARNING(
                    f"⚠️  No reviews found for business_id {business_id}"
                )
            )
            return

        issues_found = []
        
        for idx, review in enumerate(reviews[:limit], 1):
            self.stdout.write(f"\n{'─'*100}")
            self.stdout.write(f"📋 Review #{idx}: {review.google_review_id}")
            self.stdout.write(f"   Reviewer: {review.reviewer_name}")
            self.stdout.write(f"   Rating: {review.rating}⭐")
            self.stdout.write(f"{'─'*100}")
            
            # Check avatar
            self.stdout.write(f"\n👤 AVATAR CHECK:")
            if review.reviewer_avatar and review.reviewer_avatar.name:
                avatar_path = review.reviewer_avatar.name
                self.stdout.write(f"   Database path: {avatar_path}")
                
                # Check path format
                if not avatar_path.startswith("originals/"):
                    self.stdout.write(
                        self.style.ERROR(
                            f"   ✗ ERROR: Path should start with 'originals/' but is: {avatar_path}"
                        )
                    )
                    issues_found.append(f"Review {review.google_review_id}: Avatar path incorrect")
                else:
                    self.stdout.write(
                        self.style.SUCCESS("   ✓ Path format correct (starts with 'originals/')")
                    )
                
                # Expected processed path
                if avatar_path.startswith("originals/"):
                    base_path, ext = os.path.splitext(avatar_path)
                    expected_thumb = base_path.replace("originals/", "public/thumb/", 1) + ".webp"
                    expected_url = f"{settings.CLOUDFRONT_DOMAIN}/{expected_thumb}"
                    
                    self.stdout.write(f"   Original extension: {ext}")
                    self.stdout.write(f"   Expected thumb path: {expected_thumb}")
                    self.stdout.write(f"   Expected URL: {expected_url}")
                    
                    if check_s3 and s3_client and bucket_name:
                        from botocore.exceptions import ClientError
                        
                        # Check if original exists
                        try:
                            s3_client.head_object(Bucket=bucket_name, Key=avatar_path)
                            self.stdout.write(
                                self.style.SUCCESS(f"   ✓ Original exists in S3: {avatar_path}")
                            )
                        except ClientError as e:
                            if e.response['Error']['Code'] == '404':
                                self.stdout.write(
                                    self.style.ERROR(f"   ✗ Original NOT found in S3: {avatar_path}")
                                )
                                issues_found.append(f"Review {review.google_review_id}: Avatar original missing in S3")
                            else:
                                self.stdout.write(
                                    self.style.ERROR(f"   ✗ S3 error checking original: {e}")
                                )
                        
                        # Check if processed thumb exists
                        try:
                            s3_client.head_object(Bucket=bucket_name, Key=expected_thumb)
                            self.stdout.write(
                                self.style.SUCCESS(f"   ✓ Processed thumb exists in S3: {expected_thumb}")
                            )
                        except ClientError as e:
                            if e.response['Error']['Code'] == '404':
                                self.stdout.write(
                                    self.style.ERROR(
                                        f"   ✗ Processed thumb NOT found in S3: {expected_thumb}"
                                    )
                                )
                                self.stdout.write(
                                    self.style.WARNING(
                                        "      💡 This means the Lambda has NOT processed this image yet"
                                    )
                                )
                                issues_found.append(f"Review {review.google_review_id}: Avatar thumb not processed by Lambda")
                            else:
                                self.stdout.write(
                                    self.style.ERROR(f"   ✗ S3 error checking thumb: {e}")
                                )
            else:
                self.stdout.write(self.style.NOTICE("   ℹ️  No avatar for this review"))
            
            # Check review images
            self.stdout.write(f"\n🖼️  REVIEW IMAGES CHECK:")
            if review.image_urls and len(review.image_urls) > 0:
                self.stdout.write(f"   Image count in database: {len(review.image_urls)}")
                
                for img_idx, img_path in enumerate(review.image_urls, 1):
                    self.stdout.write(f"\n   📷 Image {img_idx}:")
                    self.stdout.write(f"      Database path: {img_path}")
                    
                    # Check path format
                    if not img_path.startswith("originals/"):
                        self.stdout.write(
                            self.style.ERROR(
                                f"      ✗ ERROR: Path should start with 'originals/' but is: {img_path}"
                            )
                        )
                        issues_found.append(f"Review {review.google_review_id}: Image {img_idx} path incorrect")
                    else:
                        self.stdout.write(
                            self.style.SUCCESS("      ✓ Path format correct")
                        )
                    
                    if img_path.startswith("originals/"):
                        base_path, ext = os.path.splitext(img_path)
                        expected_thumb = base_path.replace("originals/", "public/thumb/", 1) + ".webp"
                        expected_medium = base_path.replace("originals/", "public/medium/", 1) + ".webp"
                        
                        self.stdout.write(f"      Original extension: {ext}")
                        self.stdout.write(f"      Expected thumb: {expected_thumb}")
                        self.stdout.write(f"      Expected medium: {expected_medium}")
                        
                        if check_s3 and s3_client and bucket_name:
                            from botocore.exceptions import ClientError
                            
                            # Check original
                            try:
                                s3_client.head_object(Bucket=bucket_name, Key=img_path)
                                self.stdout.write(
                                    self.style.SUCCESS(f"      ✓ Original exists in S3")
                                )
                            except ClientError as e:
                                if e.response['Error']['Code'] == '404':
                                    self.stdout.write(
                                        self.style.ERROR(f"      ✗ Original NOT found in S3")
                                    )
                                    issues_found.append(f"Review {review.google_review_id}: Image {img_idx} original missing")
                            
                            # Check thumb
                            try:
                                s3_client.head_object(Bucket=bucket_name, Key=expected_thumb)
                                self.stdout.write(
                                    self.style.SUCCESS(f"      ✓ Thumb exists in S3")
                                )
                            except ClientError as e:
                                if e.response['Error']['Code'] == '404':
                                    self.stdout.write(
                                        self.style.ERROR(f"      ✗ Thumb NOT found in S3")
                                    )
                                    self.stdout.write(
                                        self.style.WARNING(
                                            "         💡 Lambda hasn't processed this image"
                                        )
                                    )
                                    issues_found.append(f"Review {review.google_review_id}: Image {img_idx} thumb not processed")
                            
                            # Check medium
                            try:
                                s3_client.head_object(Bucket=bucket_name, Key=expected_medium)
                                self.stdout.write(
                                    self.style.SUCCESS(f"      ✓ Medium exists in S3")
                                )
                            except ClientError as e:
                                if e.response['Error']['Code'] == '404':
                                    self.stdout.write(
                                        self.style.ERROR(f"      ✗ Medium NOT found in S3")
                                    )
                                    issues_found.append(f"Review {review.google_review_id}: Image {img_idx} medium not processed")
            else:
                self.stdout.write(self.style.NOTICE("   ℹ️  No review images"))

        # Summary
        self.stdout.write(f"\n{'='*100}")
        self.stdout.write(f"📊 SUMMARY")
        self.stdout.write(f"{'='*100}")
        self.stdout.write(f"Reviews checked: {min(limit, reviews.count())}")
        self.stdout.write(f"Issues found: {len(issues_found)}")
        
        if issues_found:
            self.stdout.write(f"\n❌ ISSUES DETECTED:")
            for issue in issues_found:
                self.stdout.write(f"   • {issue}")
            
            self.stdout.write(f"\n💡 RECOMMENDED ACTIONS:")
            
            if any("path incorrect" in issue for issue in issues_found):
                self.stdout.write(
                    "   1. Some image paths don't start with 'originals/'"
                )
                self.stdout.write(
                    "      → Check your import_google_reviews.py script"
                )
                self.stdout.write(
                    "      → Ensure images are saved to 'originals/reviews/'"
                )
            
            if any("missing in S3" in issue for issue in issues_found):
                self.stdout.write(
                    "   2. Original images are missing from S3"
                )
                self.stdout.write(
                    "      → Re-run the import command to upload images"
                )
                self.stdout.write(
                    "      → Check if the download_image function is working"
                )
            
            if any("not processed" in issue for issue in issues_found):
                self.stdout.write(
                    "   3. Lambda hasn't processed some images"
                )
                self.stdout.write(
                    "      → Check if Lambda is triggered on S3 uploads to 'originals/'"
                )
                self.stdout.write(
                    "      → Review Lambda CloudWatch logs for errors"
                )
                self.stdout.write(
                    "      → Verify Lambda has permissions: s3:GetObject and s3:PutObject"
                )
                self.stdout.write(
                    "      → Check Lambda timeout (should be 30s+ for large images)"
                )
        else:
            self.stdout.write(
                self.style.SUCCESS("\n✅ No issues detected! Image setup looks correct.")
            )
            
            if not check_s3:
                self.stdout.write(
                    self.style.NOTICE(
                        "\n💡 Run with --check-s3 flag to verify files actually exist in S3"
                    )
                )
        
        self.stdout.write(f"{'='*100}\n")
