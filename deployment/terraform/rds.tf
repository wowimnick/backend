resource "random_password" "prod_db" {
  length  = 32
  special = false
}

resource "random_password" "staging_db" {
  count   = var.enable_staging ? 1 : 0
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-db-subnets"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${var.name_prefix}-db-subnets" }
}

resource "aws_db_parameter_group" "postgres16" {
  name   = "${var.name_prefix}-postgres16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "prod" {
  identifier     = local.prod_db_id
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 50
  max_allocated_storage = 200
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "classeasily"
  username = "classeasily"
  password = random_password.prod_db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.postgres16.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false
  deletion_protection    = true
  skip_final_snapshot    = false
  backup_retention_period = 7
  backup_window           = "07:00-08:00"
  maintenance_window      = "sun:08:00-sun:09:00"
  auto_minor_version_upgrade = true
  performance_insights_enabled = false
  copy_tags_to_snapshot        = true

  tags = {
    Name        = local.prod_db_id
    Environment = "prod"
  }

  lifecycle {
    ignore_changes = [password, engine_version]
  }
}

resource "aws_db_instance" "staging" {
  count          = var.enable_staging ? 1 : 0
  identifier     = local.staging_db_id
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.staging_db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "classeasily"
  username = "classeasily"
  password = random_password.staging_db[0].result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.postgres16.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false
  deletion_protection    = false
  skip_final_snapshot    = true
  backup_retention_period = 1
  auto_minor_version_upgrade = true

  tags = {
    Name        = local.staging_db_id
    Environment = "staging"
  }

  lifecycle {
    ignore_changes = [password, engine_version]
  }
}
