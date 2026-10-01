resource "aws_efs_file_system" "typesense" {
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${var.name_prefix}-prod-typesense-efs" }
}

resource "aws_efs_mount_target" "typesense" {
  count           = 2
  file_system_id  = aws_efs_file_system.typesense.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = local.cloudmap_ns
  description = "Internal DNS for Typesense (TYPESENSE_HOST=typesense.classeasily.local)"
  vpc         = aws_vpc.this.id
}

resource "aws_service_discovery_service" "typesense" {
  name = "typesense"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_cloudwatch_log_group" "typesense" {
  name              = "/ecs/${var.name_prefix}-prod-typesense"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "typesense" {
  family                   = local.typesense_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.typesense_task.arn

  volume {
    name = "typesense-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.typesense.id
      transit_encryption = "ENABLED"
      authorization_config {
        iam = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "typesense"
      image     = var.typesense_image
      essential = true
      portMappings = [{
        containerPort = 8108
        protocol      = "tcp"
      }]
      environment = [
        { name = "TYPESENSE_DATA_DIR", value = "/data" },
        { name = "TYPESENSE_ENABLE_CORS", value = "true" },
        { name = "TYPESENSE_RESET_PEERS_ON_ERROR", value = "true" },
      ]
      secrets = [
        {
          name      = "TYPESENSE_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.prod_env.arn}:TYPESENSE_API_KEY::"
        }
      ]
      mountPoints = [
        {
          sourceVolume  = "typesense-data"
          containerPath = "/data"
          readOnly      = false
        }
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "wget -q -O- http://127.0.0.1:8108/health | grep -q ok || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.typesense.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "typesense" {
  name                               = local.typesense_service
  cluster                            = aws_ecs_cluster.prod.id
  task_definition                    = aws_ecs_task_definition.typesense.arn
  desired_count                      = var.typesense_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.typesense.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.typesense.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_efs_mount_target.typesense]

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
