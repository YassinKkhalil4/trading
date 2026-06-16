terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = var.project_name
  common_env = [
    { name = "ENVIRONMENT_MODE", value = "paper" },
    { name = "DEPLOYMENT_TARGET", value = "aws" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "REDIS_URL", value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0" },
    { name = "RAW_ARCHIVE_BUCKET", value = aws_s3_bucket.raw_archive.bucket },
    { name = "ALPACA_PRIMARY_DATA_FEED", value = "iex" },
    { name = "ALPACA_ORDER_MAX_ATTEMPTS", value = "3" },
    { name = "ALPACA_ORDER_RETRY_BACKOFF_SECONDS", value = "0.25" },
    { name = "MAX_VOLATILITY_SCORE", value = "95" },
    { name = "ALLOW_LIVE_TRADING", value = "false" },
    { name = "ENABLE_LIVE_ORDER_PATH", value = "false" }
  ]
  service_commands = {
    api            = ["uvicorn", "trading_system.app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    scheduler      = ["python", "-m", "trading_system.app.services.worker", "scheduler"]
    market_stream  = ["python", "-m", "trading_system.app.services.worker", "market-stream"]
    reconciliation = ["python", "-m", "trading_system.app.services.worker", "reconciliation"]
    trade_monitor  = ["python", "-m", "trading_system.app.services.worker", "trade-monitor"]
    reviews        = ["python", "-m", "trading_system.app.services.worker", "reviews"]
    learning       = ["python", "-m", "trading_system.app.services.worker", "learning"]
  }
  load_balanced_services = {
    api = 8000
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${local.name}-nat" }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  dynamic "ingress" {
    for_each = var.enable_https ? [1] : []
    content {
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name   = "${local.name}-app"
  vpc_id = aws_vpc.main.id
  ingress {
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
}

resource "aws_security_group" "data" {
  name   = "${local.name}-data"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_security_group" "vpc_endpoint" {
  name   = "${local.name}-vpc-endpoint"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3-endpoint" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
  ])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoint.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}-endpoint" }
}

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_s3_bucket" "raw_archive" {
  bucket = var.raw_archive_bucket
}

resource "aws_s3_bucket_public_access_block" "raw_archive" {
  bucket                  = aws_s3_bucket.raw_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  rule {
    id     = "raw-payload-retention"
    status = "Enabled"
    filter {
      prefix = ""
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    expiration {
      days = 2555
    }
  }
}

resource "aws_secretsmanager_secret" "runtime" {
  name = "${local.name}/runtime"
}

resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id = aws_secretsmanager_secret.runtime.id
  secret_string = jsonencode({
    DATABASE_URL            = "postgresql+psycopg://${var.db_username}:${var.db_password}@${aws_db_instance.postgres.address}:5432/${var.db_name}"
    ADMIN_PASSWORD          = var.admin_bootstrap_password
    API_ADMIN_TOKEN         = var.api_admin_token
    ADMIN_SESSION_SECRET    = var.admin_session_secret
    ALPACA_PAPER_API_KEY    = var.alpaca_paper_api_key
    ALPACA_PAPER_SECRET_KEY = var.alpaca_paper_secret_key
    ALPACA_LIVE_API_KEY     = var.alpaca_live_api_key
    ALPACA_LIVE_SECRET_KEY  = var.alpaca_live_secret_key
  })
}

resource "aws_db_subnet_group" "postgres" {
  name       = "${local.name}-postgres"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "postgres" {
  identifier                = "${local.name}-postgres"
  engine                    = "postgres"
  engine_version          = "16"
  instance_class            = "db.t4g.micro"
  allocated_storage         = 50
  db_name                   = var.db_name
  username                  = var.db_username
  password                  = var.db_password
  db_subnet_group_name      = aws_db_subnet_group.postgres.name
  vpc_security_group_ids    = [aws_security_group.data.id]
  backup_retention_period = 14
  deletion_protection     = true
  storage_encrypted       = true
  publicly_accessible     = false
  skip_final_snapshot     = false
  final_snapshot_identifier = "${local.name}-postgres-final-snapshot"
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "Redis cache and coordination for ${local.name}"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.t4g.micro"
  num_cache_clusters         = 1
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.data.id]
  port                       = 6379
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  automatic_failover_enabled = false
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role" "task" {
  name = "${local.name}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "secrets" {
  name = "${local.name}-secrets"
  role = aws_iam_role.task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.runtime.arn
    }]
  })
}

resource "aws_iam_role_policy" "task_runtime_access" {
  name = "${local.name}-task-runtime-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.raw_archive.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.raw_archive.arn}/*"
      }
    ]
  })
}

resource "aws_ecs_task_definition" "service" {
  for_each                 = local.service_commands
  family                   = "${local.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = each.key
    image     = var.container_image
    command   = each.value
    essential = true
    portMappings = each.key == "api" ? [{
      containerPort = 8000
      hostPort      = 8000
      protocol      = "tcp"
    }] : []
    environment = local.common_env
    secrets = [
      { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:DATABASE_URL::" },
      { name = "ADMIN_PASSWORD", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ADMIN_PASSWORD::" },
      { name = "API_ADMIN_TOKEN", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:API_ADMIN_TOKEN::" },
      { name = "ADMIN_SESSION_SECRET", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ADMIN_SESSION_SECRET::" },
      { name = "ALPACA_PAPER_API_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ALPACA_PAPER_API_KEY::" },
      { name = "ALPACA_PAPER_SECRET_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ALPACA_PAPER_SECRET_KEY::" },
      { name = "ALPACA_LIVE_API_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ALPACA_LIVE_API_KEY::" },
      { name = "ALPACA_LIVE_SECRET_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:ALPACA_LIVE_SECRET_KEY::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_lb" "app" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "service" {
  for_each    = local.load_balanced_services
  name        = "${local.name}-${each.key}"
  port        = each.value
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  health_check {
    path                = each.key == "api" ? "/health" : "/"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200-399"
  }
}

resource "aws_lb_listener" "api_http_forward" {
  count             = var.enable_https ? 0 : 1
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.service["api"].arn
  }
}

resource "aws_lb_listener" "api_http_redirect" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "api_https" {
  count             = var.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.service["api"].arn
  }
}

resource "aws_ecs_service" "load_balanced" {
  for_each        = local.load_balanced_services
  name            = "${local.name}-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.service[each.key].arn
    container_name   = each.key
    container_port   = each.value
  }
}

resource "aws_ecs_service" "worker" {
  for_each = {
    for name, command in local.service_commands : name => command
    if !contains(keys(local.load_balanced_services), name)
  }

  name            = "${local.name}-${each.key}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.service[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
}

resource "aws_wafv2_web_acl" "app" {
  name  = "${local.name}-waf"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-common-rules"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "app" {
  resource_arn = aws_lb.app.arn
  web_acl_arn  = aws_wafv2_web_acl.app.arn
}
