from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN_TF = ROOT / "infra" / "aws" / "main.tf"
VARIABLES_TF = ROOT / "infra" / "aws" / "variables.tf"
CI_CD = ROOT / ".github" / "workflows" / "ci-cd.yml"
DOCKER_COMPOSE = ROOT / "docker-compose.yml"
WORKER = ROOT / "trading_system" / "app" / "services" / "worker.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_terraform_declares_required_aws_runtime_resources():
    main_tf = _read(MAIN_TF)
    variables_tf = _read(VARIABLES_TF)
    required_fragments = [
        'provider "aws"',
        'default = "us-east-1"',
        'resource "aws_ecs_cluster"',
        'resource "aws_ecs_task_definition"',
        'requires_compatibilities = ["FARGATE"]',
        'resource "aws_ecs_service" "load_balanced"',
        'resource "aws_ecs_service" "worker"',
        'resource "aws_subnet" "private"',
        'resource "aws_nat_gateway" "main"',
        'resource "aws_vpc_endpoint" "s3"',
        'resource "aws_vpc_endpoint" "interface"',
        'resource "aws_db_instance" "postgres"',
        'engine_version          = "16"',
        'resource "aws_elasticache_replication_group" "redis"',
        'engine_version             = "7.1"',
        'resource "aws_s3_bucket" "raw_archive"',
        'resource "aws_secretsmanager_secret" "runtime"',
        'resource "aws_cloudwatch_log_group" "app"',
        'resource "aws_lb" "app"',
        'resource "aws_wafv2_web_acl" "app"',
        'resource "aws_ecr_repository" "app"',
    ]

    combined = f"{main_tf}\n{variables_tf}"

    for fragment in required_fragments:
        assert fragment in combined, fragment


def test_terraform_deploys_required_independent_services_with_live_disabled_defaults():
    main_tf = _read(MAIN_TF)
    required_services = {
        "api",
        "dashboard",
        "scheduler",
        "market_stream",
        "reconciliation",
        "trade_monitor",
        "review",
        "learning",
    }

    for service in required_services:
        assert f"{service}" in main_tf, service

    assert '{ name = "ENVIRONMENT_MODE", value = "paper" }' in main_tf
    assert 'value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"' in main_tf
    assert '{ name = "ALLOW_LIVE_TRADING", value = "false" }' in main_tf
    assert '{ name = "ENABLE_LIVE_ORDER_PATH", value = "false" }' in main_tf
    assert 'path                = each.key == "api" ? "/health" : "/"' in main_tf
    assert 'path                = each.key == "api" ? "/ops/health" : "/"' not in main_tf


def test_terraform_keeps_runtime_and_data_services_private_with_nat_egress():
    main_tf = _read(MAIN_TF)
    expected_fragments = [
        'resource "aws_subnet" "private"',
        "map_public_ip_on_launch = false",
        'resource "aws_nat_gateway" "main"',
        "nat_gateway_id = aws_nat_gateway.main.id",
        "subnet_ids = aws_subnet.private[*].id",
        "subnets          = aws_subnet.private[*].id",
        "assign_public_ip = false",
        "publicly_accessible     = false",
        "subnets            = aws_subnet.public[*].id",
    ]

    for fragment in expected_fragments:
        assert fragment in main_tf, fragment

    assert "assign_public_ip = true" not in main_tf
    assert "subnet_ids = aws_subnet.public[*].id" not in main_tf


def test_terraform_private_tasks_have_aws_service_endpoints():
    main_tf = _read(MAIN_TF)
    endpoint_fragments = [
        'resource "aws_security_group" "vpc_endpoint"',
        "security_groups = [aws_security_group.app.id]",
        'service_name      = "com.amazonaws.${var.aws_region}.s3"',
        'vpc_endpoint_type = "Gateway"',
        "route_table_ids   = [aws_route_table.private.id]",
        '"ecr.api"',
        '"ecr.dkr"',
        '"logs"',
        '"secretsmanager"',
        'vpc_endpoint_type   = "Interface"',
        "subnet_ids          = aws_subnet.private[*].id",
        "security_group_ids  = [aws_security_group.vpc_endpoint.id]",
        "private_dns_enabled = true",
    ]

    for fragment in endpoint_fragments:
        assert fragment in main_tf, fragment


def test_terraform_preserves_data_and_raw_archive_safety_controls():
    main_tf = _read(MAIN_TF)
    safety_fragments = [
        "backup_retention_period = 14",
        "deletion_protection     = true",
        "storage_encrypted       = true",
        "skip_final_snapshot     = false",
        "at_rest_encryption_enabled = true",
        "transit_encryption_enabled = true",
        "block_public_acls       = true",
        "block_public_policy     = true",
        "restrict_public_buckets = true",
        'sse_algorithm = "AES256"',
        'versioning_configuration { status = "Enabled" }',
        "retention_in_days = 30",
    ]

    for fragment in safety_fragments:
        assert fragment in main_tf, fragment


def test_github_actions_runs_tests_builds_ecr_migrates_then_rolls_services():
    workflow = _read(CI_CD)
    expected_services = "api dashboard scheduler market_stream reconciliation trade_monitor reviews learning"

    assert "pytest trading_system/tests -q" in workflow
    assert "aws-actions/configure-aws-credentials@v4" in workflow
    assert "aws-actions/amazon-ecr-login@v2" in workflow
    assert 'docker build -t "$REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG" .' in workflow
    assert 'docker push "$REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG"' in workflow
    assert f"SERVICES: {expected_services}" in workflow
    assert '"command":["alembic","-c","trading_system/alembic.ini","upgrade","head"]' in workflow
    assert "assignPublicIp=DISABLED" in workflow
    assert "assignPublicIp=ENABLED" not in workflow
    assert workflow.index("Run Alembic migration task") < workflow.index("Roll ECS services")


def test_docker_compose_preserves_local_service_parity():
    compose = _read(DOCKER_COMPOSE)
    expected_fragments = [
        "image: postgres:16",
        "image: redis:7",
        "api:",
        "dashboard:",
        "scheduler-worker:",
        "market-stream-worker:",
        "reconciliation-worker:",
        "trade-monitor-worker:",
        "review-worker:",
        "learning-worker:",
        "command: python -m trading_system.app.services.worker scheduler",
        "command: python -m trading_system.app.services.worker market-stream",
        "command: python -m trading_system.app.services.worker reconciliation",
        "command: python -m trading_system.app.services.worker trade-monitor",
        "command: python -m trading_system.app.services.worker review",
        "command: python -m trading_system.app.services.worker learning",
    ]

    for fragment in expected_fragments:
        assert fragment in compose, fragment


def test_review_worker_name_matches_production_service_with_legacy_alias():
    worker = _read(WORKER)

    assert '"review"' in worker
    assert 'worker in {"review", "reviews"}' in worker
