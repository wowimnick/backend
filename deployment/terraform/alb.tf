resource "aws_lb" "shared" {
  name               = local.alb_name
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  idle_timeout = 60

  tags = { Name = local.alb_name }
}

resource "aws_lb_target_group" "prod_web" {
  name        = "${var.name_prefix}-prod-web-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health-check/"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

resource "aws_lb_target_group" "staging_web" {
  count       = var.enable_staging ? 1 : 0
  name        = "${var.name_prefix}-staging-web-tg"
  port        = 80
  protocol    = "HTTP"
  vpc_id      = aws_vpc.this.id
  target_type = "instance"

  health_check {
    enabled             = true
    path                = "/health-check/"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.shared.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = var.alb_certificate_arn != "" ? "redirect" : "forward"

    dynamic "redirect" {
      for_each = var.alb_certificate_arn != "" ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    dynamic "forward" {
      for_each = var.alb_certificate_arn == "" ? [1] : []
      content {
        target_group {
          arn = aws_lb_target_group.prod_web.arn
        }
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = var.alb_certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.shared.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.alb_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.prod_web.arn
  }
}

resource "aws_lb_listener_rule" "staging_https" {
  count        = var.enable_staging && var.alb_certificate_arn != "" ? 1 : 0
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.staging_web[0].arn
  }

  condition {
    host_header {
      values = [var.staging_api_domain]
    }
  }
}

resource "aws_lb_listener_rule" "staging_http" {
  count        = var.enable_staging && var.alb_certificate_arn == "" ? 1 : 0
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.staging_web[0].arn
  }

  condition {
    host_header {
      values = [var.staging_api_domain]
    }
  }
}

resource "aws_route53_record" "api" {
  count   = var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.api_domain
  type    = "A"

  alias {
    name                   = aws_lb.shared.dns_name
    zone_id                = aws_lb.shared.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "staging_api" {
  count   = var.enable_staging && var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.staging_api_domain
  type    = "A"

  alias {
    name                   = aws_lb.shared.dns_name
    zone_id                = aws_lb.shared.zone_id
    evaluate_target_health = true
  }
}
