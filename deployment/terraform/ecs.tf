resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${var.name_prefix}-prod-web"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${var.name_prefix}-prod-worker"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "prod" {
  name = local.cluster_name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "prod" {
  cluster_name       = aws_ecs_cluster.prod.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_ecs_task_definition" "web" {
  family                   = local.web_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "web"
      image     = local.image_uri
      essential = true
      cpu       = 256
      memory    = 1024
      portMappings = [{
        containerPort = 8000
        protocol      = "tcp"
      }]
      environment = [
        { name = "IS_DOCKER", value = "true" },
        { name = "CONTAINER_ROLE", value = "web" },
        { name = "DJANGO_ENV", value = "prod" },
        { name = "DEBUG", value = "False" },
        { name = "BUILD_ID", value = var.app_image_tag },
      ]
      secrets = local.django_secrets
      command = [
        "gunicorn",
        "CEBackend.asgi:application",
        "-w", "1",
        "-k", "uvicorn.workers.UvicornWorker",
        "-b", "0.0.0.0:8000",
      ]
      healthCheck = {
        command     = ["CMD-SHELL", "curl -sf http://127.0.0.1:8000/health-check/ >/dev/null || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 120
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.web.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = local.worker_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image_uri
      essential = true
      cpu       = 256
      memory    = 1024
      environment = [
        { name = "IS_DOCKER", value = "true" },
        { name = "CONTAINER_ROLE", value = "worker" },
        { name = "DJANGO_ENV", value = "prod" },
        { name = "DEBUG", value = "False" },
        { name = "BUILD_ID", value = var.app_image_tag },
      ]
      secrets = local.django_secrets
      command = ["/usr/bin/supervisord", "-n"]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "web" {
  name                               = local.web_service
  cluster                            = aws_ecs_cluster.prod.id
  task_definition                    = aws_ecs_task_definition.web.arn
  desired_count                      = var.web_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  health_check_grace_period_seconds  = var.web_health_check_grace_period
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.prod_web.arn
    container_name   = "web"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  # CI (GitHub Actions or CodePipeline) registers a new revision with the git SHA
  # image, same as prod-backend-deploy.yml. Terraform keeps the service shape.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name                               = local.worker_service
  cluster                            = aws_ecs_cluster.prod.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = var.worker_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
