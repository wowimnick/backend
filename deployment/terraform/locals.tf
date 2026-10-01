data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "codepipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

locals {
  account_id = coalesce(var.aws_account_id, data.aws_caller_identity.current.account_id)
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)

  cluster_name       = "${var.name_prefix}-prod-cluster"
  ecr_name           = var.ecr_repository_name
  alb_name           = "${var.name_prefix}-shared-alb"
  web_service        = "${var.name_prefix}-prod-web-service"
  worker_service     = "${var.name_prefix}-prod-worker-service"
  typesense_service  = "${var.name_prefix}-prod-typesense-service"
  web_task_family    = "${var.name_prefix}-prod-backend-web-task"
  worker_task_family = "${var.name_prefix}-prod-backend-task-worker"
  typesense_family   = "${var.name_prefix}-prod-typesense-task"
  prod_db_id         = "${var.name_prefix}-prod-database-solo"
  staging_db_id      = "${var.name_prefix}-staging-db"
  staging_ec2_name   = "${var.name_prefix}-staging-server"
  cloudmap_ns        = "classeasily.local"
  typesense_dns      = "typesense.classeasily.local"
  prod_secret_name   = "classeasily/prod/env"
  staging_secret_name = "classeasily/staging/env"

  github_repo = "${var.github_org}/${var.github_backend_repo}"
  image_uri   = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${local.ecr_name}:${var.app_image_tag}"

  # JSON keys injected into Fargate task defs from Secrets Manager (same set staging-deploy.yml writes).
  django_secret_keys = [
    "DJANGO_SECRET_KEY",
    "ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "SITE_DOMAIN",
    "SECURE_SSL_REDIRECT",
    "REVALIDATION_SECRET",
    "VERCEL_AUTOMATION_BYPASS_SECRET",
    "SENTRY_DSN",
    "GEMINI_API_KEY",
    "META_CAPI_ENABLED",
    "META_CAPI_TEST_EVENT_CODE",
    "META_PIXEL_ID",
    "META_CAPI_ACCESS_TOKEN",
    "DB_ENGINE",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
    "CACHE_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "AWS_STORAGE_BUCKET_NAME",
    "AWS_S3_REGION_NAME",
    "CLOUDFRONT_DOMAIN",
    "AWS_SNS_REGION",
    "AWS_SMS_ENABLED",
    "RESEND_API_KEY",
    "APIFY_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "STRIPE_PUBLIC_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_CONNECT_WEBHOOK_SECRET",
    "STRIPE_PAYMENTS_WEBHOOK_SECRET",
    "OPENROUTER_API_KEY",
    "WIDGET_SUBSCRIPTION_REQUIRED",
    "WIDGET_SUBSCRIPTION_PRICE_ADVANCED",
    "WIDGET_SUBSCRIPTION_PRICE_GROWTH",
    "WIDGET_SUBSCRIPTION_PRICE_BASIC",
    "MARKETPLACE_EMAIL_ADDON_PRICE_ID",
    "EMAIL_MARKETING_STARTER_PRICE_ID",
    "EMAIL_MARKETING_GROWTH_PRICE_ID",
    "EMAIL_MARKETING_BUSINESS_PRICE_ID",
    "EMAIL_MARKETING_SCALE_PRICE_ID",
    "FRONTEND_URL",
    "DEFAULT_FROM_EMAIL",
    "TYPESENSE_HOST",
    "TYPESENSE_PORT",
    "TYPESENSE_PROTOCOL",
    "TYPESENSE_API_KEY",
    "TYPESENSE_COLLECTION_ALIAS",
    "TYPESENSE_AUTO_BOOTSTRAP",
    "TYPESENSE_FULL_REINDEX_EACH_DEPLOY",
  ]

  django_secrets = [
    for key in local.django_secret_keys : {
      name      = key
      valueFrom = "${aws_secretsmanager_secret.prod_env.arn}:${key}::"
    }
  ]

  public_subnet_cidrs  = [cidrsubnet(var.vpc_cidr, 8, 0), cidrsubnet(var.vpc_cidr, 8, 1)]
  private_subnet_cidrs = [cidrsubnet(var.vpc_cidr, 8, 10), cidrsubnet(var.vpc_cidr, 8, 11)]

  github_oidc_arn   = coalesce(nullif(var.github_oidc_provider_arn, ""), one(aws_iam_openid_connect_provider.github[*].arn))
  pipelines_enabled = var.enable_codepipeline && var.codestar_connection_arn != ""
}
