resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-redis-subnets"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "shared" {
  replication_group_id = "${var.name_prefix}-shared-redis"
  description          = "Shared Redis. Django: prod uses DBs /3 /4 /5, staging /0 /1 /2"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_clusters   = 1
  port                 = 6379
  parameter_group_name = "default.redis7"

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  automatic_failover_enabled = false
  multi_az_enabled           = false
  apply_immediately          = true

  tags = { Name = "${var.name_prefix}-shared-redis" }
}
