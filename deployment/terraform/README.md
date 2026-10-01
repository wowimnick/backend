# ClassEasily AWS — Terraform equivalent of the GitHub Actions deploy pipeline

Maps the live us-east-2 topology and the three backend workflows onto Terraform:

| Today (GitHub Actions) | This stack |
|---|---|
| `prod-backend-deploy.yml` (push to `prod`) | `aws_codepipeline.prod` + `aws_codebuild_project.prod` (`buildspec/prod.yml`) |
| `staging-deploy.yml` (push to `main`) | `aws_codepipeline.staging` + SSM document `classeasily-staging-deploy` |
| `typesense-ecs-deploy.yml` (`workflow_dispatch`) | `aws_codepipeline.typesense` (manual approval) or `terraform apply -var typesense_image=...` |
| `secrets.AWS_ROLE_TO_ASSUME` | `aws_iam_role.github_actions` (OIDC kept so current workflows still work) |

Frontend stays on Vercel. This module is the AWS API / data plane only.

Resource names match production: `classeasily-prod-cluster`, `classeasily-backend-staging` ECR, `classeasily-shared-alb`, `classeasily-prod-database-solo`, Cloud Map `typesense.classeasily.local`.

## Cost (current account, July 2026)

This is a 1:1 model of the existing account, not a new architecture. Last full month was about **$240**. Dominated by NAT, RDS `db.t4g.small`, Fargate (web 0.25 vCPU / 1 GB, worker same, Typesense 0.5 vCPU / 1 GB), ALB, and ECR storage. ECR lifecycle here keeps 20 images to cut the ~$26 image-storage line.

## Apply (greenfield)

```bash
cd deployment/terraform
cp terraform.tfvars.example terraform.tfvars
# set media_bucket_name, alb_certificate_arn, optional CodeStar ARN
terraform init
terraform plan
terraform apply
```

Fill `classeasily/prod/env` after the first apply (Stripe, Resend, Google, etc.). Terraform seeds TYPESENSE_*, DB_*, CACHE_URL, and CloudFront, then ignores later secret edits:

```bash
aws secretsmanager put-secret-value \
  --region us-east-2 \
  --secret-id classeasily/prod/env \
  --secret-string file://prod.env.json
```

On the Postgres instance, once: `CREATE EXTENSION IF NOT EXISTS postgis;`

## Import (existing account 459929160817)

Do **not** `apply` this as a second copy of prod. Import (or `terraform state mv`) the live cluster, ALB, RDS, EFS, ECR, SGs, then plan until the diff is only intentional. Existing IDs from ops notes:

- EFS `fs-022624328107e5b42`
- App SG `sg-0759107194bc02c3e`
- Typesense SG `sg-0bb7a671830e90e1a`
- EFS SG `sg-0641f7aa7e140c73d`

Pass `github_oidc_provider_arn` if `token.actions.githubusercontent.com` already exists.

## CI after apply

Keep GitHub Actions: set `AWS_ROLE_TO_ASSUME` to `github_actions_role_arn`.

Or enable CodePipeline: create a GitHub CodeStar connection, set `codestar_connection_arn`, apply. Prod still builds one image, patches both task defs, and sets the 900s ALB grace period.

Typesense image bumps: `terraform apply -var='typesense_image=typesense/typesense:27.0'` or run the Typesense pipeline.
