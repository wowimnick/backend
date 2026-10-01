variable "aws_region" {
  type        = string
  description = "Region for the API, data plane, and ALB (must match existing prod)."
  default     = "us-east-2"
}

variable "aws_account_id" {
  type        = string
  description = "AWS account that currently hosts ClassEasily."
  default     = "459929160817"
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix. Defaults match live prod names."
  default     = "classeasily"
}

variable "github_org" {
  type        = string
  default     = "wowimnick"
}

variable "github_backend_repo" {
  type        = string
  default     = "backend"
}

variable "prod_branch" {
  type        = string
  default     = "prod"
}

variable "staging_branch" {
  type        = string
  default     = "main"
}

variable "api_domain" {
  type        = string
  description = "Public hostname for the production ALB rule (Django ALLOWED_HOSTS / CSRF)."
  default     = "api.classeasily.com"
}

variable "staging_api_domain" {
  type        = string
  default     = "staging-api.classeasily.com"
}

variable "alb_certificate_arn" {
  type        = string
  description = "ACM cert in us-east-2 covering api + staging API hosts. Empty skips HTTPS listener."
  default     = ""
}

variable "route53_zone_id" {
  type        = string
  description = "Optional public hosted zone for ALB alias records."
  default     = ""
}

variable "app_image_tag" {
  type        = string
  description = "Initial Django image tag. CI overwrites this on each deploy (service ignore_changes)."
  default     = "latest"
}

variable "typesense_image" {
  type        = string
  description = "Equivalent of typesense-ecs-deploy.yml input typesense_image."
  default     = "typesense/typesense:26.0"
}

variable "ecr_repository_name" {
  type        = string
  default     = "classeasily-backend-staging"
}

variable "media_bucket_name" {
  type        = string
  description = "S3 bucket for original + public media (AWS_STORAGE_BUCKET_NAME)."
}

variable "alert_email" {
  type        = string
  description = "SNS endpoint for Typesense / ALB alarms."
  default     = ""
}

variable "enable_staging" {
  type        = bool
  default     = true
}

variable "enable_codepipeline" {
  type        = bool
  description = "Create AWS CodePipeline/CodeBuild as the AWS-native stand-in for GitHub Actions."
  default     = true
}

variable "codestar_connection_arn" {
  type        = string
  description = "CodeStar connection to GitHub (required when enable_codepipeline is true)."
  default     = ""
}

variable "enable_nat_gateway" {
  type        = bool
  description = "Private Fargate egress. Set false only if you move tasks to public subnets."
  default     = true
}

variable "web_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "typesense_desired_count" {
  type    = number
  default = 1
}

variable "web_health_check_grace_period" {
  type        = number
  description = "Matches ECS_WEB_HEALTH_CHECK_GRACE_PERIOD_SECONDS in prod-backend-deploy.yml."
  default     = 900
}

variable "staging_instance_type" {
  type    = string
  default = "t3.small"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "staging_db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "github_oidc_provider_arn" {
  type        = string
  description = "Existing GitHub OIDC provider ARN. Leave empty to create one. The ClassEasily account already uses GHA OIDC."
  default     = ""
}
