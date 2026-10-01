resource "random_password" "typesense_api_key" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "prod_env" {
  name        = local.prod_secret_name
  description = "JSON blob injected into prod web/worker/Typesense task definitions"

  recovery_window_in_days = 7
}

# Placeholder only. Real values (Stripe, Django, RDS URLs, etc.) are filled outside
# Terraform so they never land in state from a tfvars file. After apply:
#   aws secretsmanager put-secret-value --secret-id classeasily/prod/env --secret-string file://prod.env.json
resource "aws_secretsmanager_secret_version" "prod_env_seed" {
  secret_id = aws_secretsmanager_secret.prod_env.id
  secret_string = jsonencode(merge(
    { for key in local.django_secret_keys : key => "" },
    {
      TYPESENSE_HOST                  = local.typesense_dns
      TYPESENSE_PORT                  = "8108"
      TYPESENSE_PROTOCOL              = "http"
      TYPESENSE_API_KEY               = random_password.typesense_api_key.result
      TYPESENSE_COLLECTION_ALIAS      = "classes_live"
      TYPESENSE_AUTO_BOOTSTRAP        = "true"
      TYPESENSE_FULL_REINDEX_EACH_DEPLOY = "false"
      AWS_STORAGE_BUCKET_NAME         = aws_s3_bucket.media.bucket
      AWS_S3_REGION_NAME              = var.aws_region
      CLOUDFRONT_DOMAIN               = aws_cloudfront_distribution.media.domain_name
      DB_ENGINE                       = "django.contrib.gis.db.backends.postgis"
      DB_PORT                         = "5432"
      DB_NAME                         = "classeasily"
      DB_USER                         = "classeasily"
      DB_HOST                         = aws_db_instance.prod.address
      DB_PASSWORD                     = random_password.prod_db.result
      CACHE_URL                       = "rediss://${aws_elasticache_replication_group.shared.primary_endpoint_address}:6379"
      SITE_DOMAIN                     = var.api_domain
    }
  ))

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "staging_env" {
  count       = var.enable_staging ? 1 : 0
  name        = local.staging_secret_name
  description = "Staging Docker env file source (replaces GitHub-secret SSH heredoc)"

  recovery_window_in_days = 7
}
