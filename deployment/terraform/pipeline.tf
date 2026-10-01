resource "aws_s3_bucket" "pipeline" {
  count  = local.pipelines_enabled ? 1 : 0
  bucket = "${var.name_prefix}-codepipeline-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  count                   = local.pipelines_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.pipeline[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "pipeline" {
  count  = local.pipelines_enabled ? 1 : 0
  bucket = aws_s3_bucket.pipeline[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  count  = local.pipelines_enabled ? 1 : 0
  bucket = aws_s3_bucket.pipeline[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_iam_role" "codebuild" {
  count              = local.pipelines_enabled ? 1 : 0
  name               = "${var.name_prefix}-codebuild"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

resource "aws_iam_role_policy" "codebuild" {
  count = local.pipelines_enabled ? 1 : 0
  name  = "${var.name_prefix}-codebuild"
  role  = aws_iam_role.codebuild[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = concat(
          [
            "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/codebuild/${var.name_prefix}-*",
            "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/codebuild/${var.name_prefix}-*:*",
          ]
        )
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:PutObject",
          "s3:GetBucketLocation",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.pipeline[0].arn,
          "${aws_s3_bucket.pipeline[0].arn}/*",
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy" "codebuild_deploy" {
  count  = local.pipelines_enabled ? 1 : 0
  name   = "${var.name_prefix}-codebuild-deploy"
  role   = aws_iam_role.codebuild[0].id
  policy = data.aws_iam_policy_document.deploy.json
}

resource "aws_iam_role" "codepipeline" {
  count              = local.pipelines_enabled ? 1 : 0
  name               = "${var.name_prefix}-codepipeline"
  assume_role_policy = data.aws_iam_policy_document.codepipeline_assume.json
}

resource "aws_iam_role_policy" "codepipeline" {
  count = local.pipelines_enabled ? 1 : 0
  name  = "${var.name_prefix}-codepipeline"
  role  = aws_iam_role.codepipeline[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:PutObject",
          "s3:GetBucketVersioning",
        ]
        Resource = [
          aws_s3_bucket.pipeline[0].arn,
          "${aws_s3_bucket.pipeline[0].arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["codestar-connections:UseConnection"]
        Resource = var.codestar_connection_arn
      },
      {
        Effect = "Allow"
        Action = [
          "codebuild:BatchGetBuilds",
          "codebuild:StartBuild",
        ]
        Resource = concat(
          [
            aws_codebuild_project.prod[0].arn,
            aws_codebuild_project.typesense[0].arn,
          ],
          aws_codebuild_project.staging[*].arn,
        )
      },
    ]
  })
}

resource "aws_codebuild_project" "prod" {
  count        = local.pipelines_enabled ? 1 : 0
  name         = "${var.name_prefix}-prod-backend-deploy"
  description  = "Equivalent of GitHub workflow Deploy Backend to Production"
  service_role = aws_iam_role.codebuild[0].arn

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = file("${path.module}/buildspec/prod.yml")
  }
}

resource "aws_codebuild_project" "staging" {
  count        = local.pipelines_enabled && var.enable_staging ? 1 : 0
  name         = "${var.name_prefix}-staging-deploy"
  description  = "Equivalent of GitHub workflow Deploy Staging to EC2"
  service_role = aws_iam_role.codebuild[0].arn

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "STAGING_INSTANCE_ID"
      value = aws_instance.staging[0].id
    }

    environment_variable {
      name  = "STAGING_SSM_DOCUMENT"
      value = aws_ssm_document.staging_deploy[0].name
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = file("${path.module}/buildspec/staging.yml")
  }
}

resource "aws_codebuild_project" "typesense" {
  count        = local.pipelines_enabled ? 1 : 0
  name         = "${var.name_prefix}-typesense-ecs-deploy"
  description  = "Equivalent of GitHub workflow Deploy Typesense (ECS)"
  service_role = aws_iam_role.codebuild[0].arn

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = false

    environment_variable {
      name  = "TYPESENSE_IMAGE"
      value = var.typesense_image
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = file("${path.module}/buildspec/typesense.yml")
  }
}

resource "aws_codepipeline" "prod" {
  count    = local.pipelines_enabled ? 1 : 0
  name     = "${var.name_prefix}-prod-backend"
  role_arn = aws_iam_role.codepipeline[0].arn

  artifact_store {
    location = aws_s3_bucket.pipeline[0].bucket
    type     = "S3"
  }

  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source"]
      configuration = {
        ConnectionArn        = var.codestar_connection_arn
        FullRepositoryId     = local.github_repo
        BranchName           = var.prod_branch
        DetectChanges        = "true"
        OutputArtifactFormat = "CODE_ZIP"
      }
    }
  }

  stage {
    name = "BuildAndDeploy"
    action {
      name            = "FargateWebAndWorker"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["source"]
      configuration = {
        ProjectName = aws_codebuild_project.prod[0].name
      }
    }
  }
}

resource "aws_codepipeline" "staging" {
  count    = local.pipelines_enabled && var.enable_staging ? 1 : 0
  name     = "${var.name_prefix}-staging"
  role_arn = aws_iam_role.codepipeline[0].arn

  artifact_store {
    location = aws_s3_bucket.pipeline[0].bucket
    type     = "S3"
  }

  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source"]
      configuration = {
        ConnectionArn        = var.codestar_connection_arn
        FullRepositoryId     = local.github_repo
        BranchName           = var.staging_branch
        DetectChanges        = "true"
        OutputArtifactFormat = "CODE_ZIP"
      }
    }
  }

  stage {
    name = "BuildAndDeploy"
    action {
      name            = "Ec2Docker"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["source"]
      configuration = {
        ProjectName = aws_codebuild_project.staging[0].name
      }
    }
  }
}

resource "aws_codepipeline" "typesense" {
  count    = local.pipelines_enabled ? 1 : 0
  name     = "${var.name_prefix}-typesense"
  role_arn = aws_iam_role.codepipeline[0].arn

  artifact_store {
    location = aws_s3_bucket.pipeline[0].bucket
    type     = "S3"
  }

  stage {
    name = "Source"
    action {
      name             = "GitHub"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["source"]
      configuration = {
        ConnectionArn        = var.codestar_connection_arn
        FullRepositoryId     = local.github_repo
        BranchName           = var.prod_branch
        DetectChanges        = "false"
        OutputArtifactFormat = "CODE_ZIP"
      }
    }
  }

  stage {
    name = "ManualGate"
    action {
      name     = "ApproveImageBump"
      category = "Approval"
      owner    = "AWS"
      provider = "Manual"
      version  = "1"
    }
  }

  stage {
    name = "Deploy"
    action {
      name            = "TypesenseFargate"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["source"]
      configuration = {
        ProjectName = aws_codebuild_project.typesense[0].name
      }
    }
  }
}
