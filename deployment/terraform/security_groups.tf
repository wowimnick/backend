resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-shared-alb-sg"
  description = "Public HTTPS/HTTP for the shared ALB"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP redirect"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-shared-alb-sg" }
}

resource "aws_security_group" "app" {
  name        = "${var.name_prefix}-prod-app-sg"
  description = "Fargate web/worker. Existing prod id: sg-0759107194bc02c3e"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "ALB to Django ASGI"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-prod-app-sg" }
}

resource "aws_security_group" "typesense" {
  name        = "${var.name_prefix}-prod-typesense-sg"
  description = "Typesense Fargate. Existing prod id: sg-0bb7a671830e90e1a"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "App to Typesense HTTP"
    from_port       = 8108
    to_port         = 8108
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-prod-typesense-sg" }
}

resource "aws_security_group" "efs" {
  name        = "${var.name_prefix}-prod-typesense-efs-sg"
  description = "EFS NFS. Existing prod id: sg-0641f7aa7e140c73d"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Typesense NFS"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.typesense.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-prod-typesense-efs-sg" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-rds-sg"
  description = "Postgres from app + staging only"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Prod Fargate and optional staging EC2"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = concat([aws_security_group.app.id], aws_security_group.staging[*].id)
  }

  tags = { Name = "${var.name_prefix}-rds-sg" }
}

resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis-sg"
  description = "Shared Redis: staging DBs 0-2, prod DBs 3-5"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = concat([aws_security_group.app.id], aws_security_group.staging[*].id)
  }

  tags = { Name = "${var.name_prefix}-redis-sg" }
}

resource "aws_security_group" "staging" {
  count       = var.enable_staging ? 1 : 0
  name        = "${var.name_prefix}-staging-ec2-sg"
  description = "Staging EC2 behind the shared ALB (no SSH; deploy via SSM)"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "ALB to staging Docker web :80"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-staging-ec2-sg" }
}
