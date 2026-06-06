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

Required variables:

- `container_image`
- `db_password`
- `admin_bootstrap_password`
- `api_admin_token`
- `admin_session_secret`
- `raw_archive_bucket`

Live trading remains disabled by default:

- `ENVIRONMENT_MODE=paper`
- `ALLOW_LIVE_TRADING=false`
- `ENABLE_LIVE_ORDER_PATH=false`

Run migrations before service rollout:

```bash
alembic -c trading_system/alembic.ini upgrade head
```

The GitHub Actions deployment workflow runs Alembic as a one-off ECS Fargate task before rolling services.
Application tasks run with a task role that can write raw provider payload archives to the configured S3 bucket.
