output "github_actions_role_arn" {
  description = "Set GitHub secret AWS_ROLE_TO_ASSUME to this value (existing GHA workflows keep working)."
  value       = aws_iam_role.github_actions.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.prod.name
}

output "alb_dns_name" {
  value = aws_lb.shared.dns_name
}

output "typesense_host" {
  value = local.typesense_dns
}

output "prod_secret_arn" {
  value = aws_secretsmanager_secret.prod_env.arn
}

output "cloudfront_domain" {
  value = aws_cloudfront_distribution.media.domain_name
}

output "prod_db_endpoint" {
  value = aws_db_instance.prod.address
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.shared.primary_endpoint_address
}

output "codepipeline_prod" {
  value = try(aws_codepipeline.prod[0].name, null)
}

output "staging_instance_id" {
  value = try(aws_instance.staging[0].id, null)
}
