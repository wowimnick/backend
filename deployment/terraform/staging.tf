data "aws_ami" "al2023" {
  count       = var.enable_staging ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_iam_role" "staging_ec2" {
  count              = var.enable_staging ? 1 : 0
  name               = "${var.name_prefix}-staging-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "staging_ssm" {
  count      = var.enable_staging ? 1 : 0
  role       = aws_iam_role.staging_ec2[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "staging_ec2" {
  count = var.enable_staging ? 1 : 0
  name  = "${var.name_prefix}-staging-ec2"
  role  = aws_iam_role.staging_ec2[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = aws_ecr_repository.backend.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.staging_env[0].arn
      }
    ]
  })
}

resource "aws_iam_instance_profile" "staging" {
  count = var.enable_staging ? 1 : 0
  name  = "${var.name_prefix}-staging-ec2"
  role  = aws_iam_role.staging_ec2[0].name
}

resource "aws_instance" "staging" {
  count                       = var.enable_staging ? 1 : 0
  ami                         = data.aws_ami.al2023[0].id
  instance_type               = var.staging_instance_type
  subnet_id                   = aws_subnet.private[0].id
  vpc_security_group_ids      = [aws_security_group.staging[0].id]
  iam_instance_profile        = aws_iam_instance_profile.staging[0].name
  associate_public_ip_address = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -eux
    dnf update -y
    dnf install -y docker jq
    systemctl enable --now docker
    usermod -aG docker ec2-user
    docker network create classeasily-net || true
    docker volume create classeasily-typesense-data || true
  EOF

  tags = {
    Name        = local.staging_ec2_name
    Environment = "staging"
  }
}

resource "aws_lb_target_group_attachment" "staging" {
  count            = var.enable_staging ? 1 : 0
  target_group_arn = aws_lb_target_group.staging_web[0].arn
  target_id        = aws_instance.staging[0].id
  port             = 80
}

# SSM document is the Terraform equivalent of the SSH block in staging-deploy.yml.
resource "aws_ssm_document" "staging_deploy" {
  count         = var.enable_staging ? 1 : 0
  name          = "${var.name_prefix}-staging-deploy"
  document_type = "Command"
  target_type   = "/AWS::EC2::Instance"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Pull SHA-tagged backend image and recreate staging Docker containers"
    parameters = {
      ImageUri = {
        type        = "String"
        description = "ECR image URI including git SHA tag"
      }
      TypesenseApiKey = {
        type        = "String"
        description = "Optional; leave blank to skip Typesense recreate"
        default     = ""
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "deploy"
        inputs = {
          timeoutSeconds = "900"
          runCommand = [
            "set -euo pipefail",
            "IMAGE_NAME='{{ ImageUri }}'",
            "REGION='${var.aws_region}'",
            "SECRET_ID='${local.staging_secret_name}'",
            "NETWORK_NAME=classeasily-net",
            "aws ecr get-login-password --region \"$REGION\" | docker login --username AWS --password-stdin ${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com",
            "docker pull \"$IMAGE_NAME\"",
            "aws secretsmanager get-secret-value --secret-id \"$SECRET_ID\" --query SecretString --output text | python3 -c 'import json,sys; d=json.load(sys.stdin); open(\"/home/ec2-user/.env.staging\",\"w\").write(\"\\n\".join(f\"{k}={v}\" for k,v in d.items())+\"\\n\")'",
            "docker network create \"$NETWORK_NAME\" || true",
            "docker volume create classeasily-typesense-data || true",
            "if [ -n '{{ TypesenseApiKey }}' ] && [ -z \"$(docker ps -aq -f name=^typesense$)\" ]; then docker run -d --restart unless-stopped --name typesense --network \"$NETWORK_NAME\" -p 127.0.0.1:8108:8108 -v classeasily-typesense-data:/data -e TYPESENSE_DATA_DIR=/data -e TYPESENSE_API_KEY='{{ TypesenseApiKey }}' -e TYPESENSE_ENABLE_CORS=true -e TYPESENSE_RESET_PEERS_ON_ERROR=true typesense/typesense:26.0; else docker start typesense || true; fi",
            "docker stop web worker || true",
            "docker rm web worker || true",
            "docker run -d --rm --env-file /home/ec2-user/.env.staging -e CONTAINER_ROLE=web -p 80:8000 --name web --network \"$NETWORK_NAME\" \"$IMAGE_NAME\" uvicorn CEBackend.asgi:application --host 0.0.0.0 --port 8000 --workers 2 --lifespan off",
            "docker run -d --rm --env-file /home/ec2-user/.env.staging -e CONTAINER_ROLE=worker --name worker --network \"$NETWORK_NAME\" \"$IMAGE_NAME\" /usr/bin/supervisord -n",
            "docker image prune -a -f || true",
          ]
        }
      }
    ]
  })
}
