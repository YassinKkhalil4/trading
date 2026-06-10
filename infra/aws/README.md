# AWS Production Deployment

This Terraform stack targets AWS `us-east-1` by default and deploys the platform in `paper` mode with live execution disabled.

Resources included:

- VPC, public/private subnets, NAT egress, VPC endpoints for S3/ECR/CloudWatch Logs/Secrets Manager, route tables, and security groups.
- ECS Fargate task definitions/services for API, dashboard, scheduler, market stream, reconciliation, trade monitor, review, and learning.
- RDS PostgreSQL 16 with automated backups, encryption, deletion protection, and a final snapshot.
- ElastiCache Redis 7.
- ECR repository.
- S3 raw payload/archive bucket with versioning, encryption, public access blocked, and lifecycle retention.
- Secrets Manager runtime secret for database URL, admin credentials, session signing, and Alpaca keys.
- ALB listeners for API and dashboard.
- WAF association.
- CloudWatch log group with retention.

Live trading remains disabled by default in task definitions:

- `ENVIRONMENT_MODE=paper`
- `ALLOW_LIVE_TRADING=false`
- `ENABLE_LIVE_ORDER_PATH=false`

## Prerequisites

- Terraform `>= 1.7.0` (project ships a binary at `.tools/bin/terraform` if not on PATH)
- AWS CLI configured for the target account (`aws sts get-caller-identity`)
- Docker (to build and push the application image)

## Variable setup

1. Copy the example file (placeholders only — no real secrets):

   ```bash
   cp infra/aws/terraform.tfvars.example infra/aws/terraform.tfvars
   ```

2. Edit `infra/aws/terraform.tfvars` with real values for your environment.

   **Do not commit `terraform.tfvars`.** It is gitignored. Only `terraform.tfvars.example` belongs in version control.

3. Required variables:

   - `aws_region`
   - `container_image`
   - `raw_archive_bucket`
   - `db_password`
   - `admin_bootstrap_password`
   - `api_admin_token`
   - `admin_session_secret`

4. Alpaca keys:

   - Set `alpaca_paper_api_key` and `alpaca_paper_secret_key` for paper-mode operation.
   - Leave `alpaca_live_api_key` and `alpaca_live_secret_key` empty unless you are explicitly enabling live testing in a controlled environment.

## Terraform workflow

Export the project Terraform binary if needed:

```bash
export PATH="/Users/yassinkhalil/Documents/Trading/.tools/bin:$PATH"
export AWS_REGION=us-east-1   # or match aws_region in terraform.tfvars
```

Initialize providers (run once per clone, or after provider version changes):

```bash
terraform -chdir=infra/aws init
```

Validate configuration syntax and provider schema (no AWS changes):

```bash
terraform -chdir=infra/aws validate
```

Review the planned infrastructure (requires AWS credentials and `terraform.tfvars`):

```bash
terraform -chdir=infra/aws plan -out=tfplan
```

Apply **only when approved** after reviewing the plan:

```bash
terraform -chdir=infra/aws apply tfplan
```

Optional formatting check:

```bash
terraform -chdir=infra/aws fmt -check
```

## ECR image build and push

After the first `terraform apply` creates the ECR repository, build and push the application image. Replace account ID, region, and tag as needed.

```bash
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="trading-intelligence"
IMAGE_TAG="$(git rev-parse HEAD)"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker build -t "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}" .
docker push "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"
```

Set `container_image` in `terraform.tfvars` to the pushed URI, then re-run `terraform plan` and `terraform apply` if task definitions need updating.

## ECS migration task

Run Alembic migrations as a one-off Fargate task **before** rolling ECS services. Use private subnet IDs and the app security group from Terraform outputs or the AWS console.

```bash
CLUSTER="trading-intelligence"
TASK_DEFINITION="trading-intelligence-api"   # latest revision after apply
SUBNETS="subnet-aaa,subnet-bbb"              # private subnets
SECURITY_GROUPS="sg-cccccccc"                # app security group

TASK_ARN="$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEFINITION" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SECURITY_GROUPS],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["alembic","-c","trading_system/alembic.ini","upgrade","head"]}]}' \
  --query 'tasks[0].taskArn' \
  --output text)"

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

aws ecs describe-tasks \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='api'].exitCode" \
  --output text
```

The GitHub Actions workflow in `.github/workflows/ci-cd.yml` runs the same migration pattern automatically on push to `main` after infrastructure exists.

## Post-deploy checks

- API health: `curl -fsS "http://<alb-dns-name>/health"`
- Dashboard: `http://<alb-dns-name>:8501`
- Confirm task environment: `ENVIRONMENT_MODE=paper`, `ALLOW_LIVE_TRADING=false`, `ENABLE_LIVE_ORDER_PATH=false`

Application tasks use a task role scoped to the raw archive S3 bucket. Runtime secrets are stored in AWS Secrets Manager and injected into ECS tasks at launch.
