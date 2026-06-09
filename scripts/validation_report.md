# Docker Validation Report

- Report version: 1.1.0
- Started: 2026-06-07T16:18:29.520102+00:00
- Finished: 2026-06-07T16:21:16.846169+00:00
- Root: `/Users/yassinkhalil/Documents/Trading`

## Passed checks

- **docker available** — PASS
  - Docker version 29.5.3, build d1c06ef6b4
- **Docker image build (API)** — PASS
  - Image trading-platform-api:validation built
  - Command: `/opt/homebrew/bin/docker build -t trading-platform-api:validation .`
- **Docker image build (dashboard)** — PASS
  - Separate dashboard tag built from shared Dockerfile
  - Command: `/opt/homebrew/bin/docker build -t trading-platform-dashboard:validation .`
- **docker-compose config validation** — PASS
  - docker-compose.yml is valid
  - Command: `/opt/homebrew/bin/docker-compose -f docker-compose.yml config`
- **docker compose up (postgres, redis, api, dashboard, workers)** — PASS
  - All compose services started
  - Command: `/opt/homebrew/bin/docker-compose -f docker-compose.yml up -d --build postgres redis api dashboard scheduler-worker market-stream-worker reconciliation-worker trade-monitor-worker review-worker learning-worker`
- **Docker service api running** — PASS
  - {"Command":"\"uvicorn trading_sys…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"7fb73ceab948","Image":"trading-api","Labels":"com.docker.compose.image.builder=classic,com.docker.compose.project.config_files=/Users/yassinkhalil/Documents/Trading/docker-compose.yml,co
- **Docker service dashboard running** — PASS
  - {"Command":"\"streamlit run tradi…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"3f5d052894ca","Image":"trading-dashboard","Labels":"com.docker.compose.project.working_dir=/Users/yassinkhalil/Documents/Trading,com.docker.compose.service=dashboard,com.docker.compose.v
- **Docker service scheduler-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"aa820f3d57ff","Image":"trading-scheduler-worker","Labels":"com.docker.compose.image=sha256:368e6e3af1b529b3b76af11b5d1335f5de532e98dd501d10130341f6e1beb555,com.docker.compose.oneoff=Fals
- **Docker service market-stream-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"2b3e13f4e36c","Image":"trading-market-stream-worker","Labels":"com.docker.compose.config-hash=e284dc01f7017f6d016d4a7209d67b5f0d32d83f726c3b12b32e3b8609562697,com.docker.compose.containe
- **Docker service reconciliation-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"fd0fe1f3ecf8","Image":"trading-reconciliation-worker","Labels":"com.docker.compose.config-hash=cfbba41dea839b78c4b9b23c4d758c57cb9d88ef75feded9e0c1fecf7056bb24,com.docker.compose.project
- **Docker service trade-monitor-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"f5354c30ef48","Image":"trading-trade-monitor-worker","Labels":"com.docker.compose.image=sha256:368e6e3af1b529b3b76af11b5d1335f5de532e98dd501d10130341f6e1beb555,com.docker.compose.image.b
- **Docker service review-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"6d4bf4a0b82e","Image":"trading-review-worker","Labels":"com.docker.compose.service=review-worker,com.docker.compose.container-number=1,com.docker.compose.image=sha256:368e6e3af1b529b3b76
- **Docker service learning-worker running** — PASS
  - {"Command":"\"python -m trading_s…\"","CreatedAt":"2026-06-07 19:20:36 +0300 EEST","ExitCode":0,"Health":"","ID":"761fbbc6c55a","Image":"trading-learning-worker","Labels":"com.docker.compose.container-number=1,com.docker.compose.depends_on=postgres:service_started:false,com.docker.compose.image=sha2
- **Alembic upgrade head inside Docker** — PASS
  - head
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps api alembic -c trading_system/alembic.ini upgrade head`
- **API /health responds through Docker** — PASS
  - {"status":"ok","message":"Production-gated platform. Live trading path is disabled unless every live gate passes."}
  - Command: `curl -fsS http://localhost:8000/health`
- **Dashboard container HTTP responds through Docker** — PASS
  - Streamlit HTTP endpoint responded
  - Command: `curl -fsS http://localhost:8501/`
- **Dashboard container starts without fake trading data** — PASS
  - counts orders=0 fills=0 positions=0
- **Docker ENVIRONMENT_MODE=paper with live gates disabled** — PASS
  - mode=paper live_order_path_enabled=False
- **Docker live order path not reachable** — PASS
  - ACCEPTED False
BLOCKERS environment_mode_live,allow_live_trading,confirm_live_trading,live_order_path_enabled,live_keys_present,active_human_approval,latest_readiness_passed,alpaca_market_data_healthy,alpaca_live_healthy,live_account_snapshot_usable,live_reconciliation_clean,strategy_approved
- **Docker worker scheduler --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps scheduler-worker python -m trading_system.app.services.worker scheduler --once`
- **Docker worker market-stream --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps market-stream-worker python -m trading_system.app.services.worker market-stream --once`
- **Docker worker reconciliation --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps reconciliation-worker python -m trading_system.app.services.worker reconciliation --once`
- **Docker worker trade-monitor --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps trade-monitor-worker python -m trading_system.app.services.worker trade-monitor --once`
- **Docker worker review --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps review-worker python -m trading_system.app.services.worker review --once`
- **Docker worker learning --once cycle** — PASS
  - completed one cycle in container
  - Command: `/opt/homebrew/bin/docker-compose run --rm --no-deps learning-worker python -m trading_system.app.services.worker learning --once`

## Failed checks

_None._

## Skipped checks

- **terraform available** — SKIP
  - terraform not installed on this host

## Commands run

- `/opt/homebrew/bin/docker build -t trading-platform-api:validation .`
- `/opt/homebrew/bin/docker build -t trading-platform-dashboard:validation .`
- `/opt/homebrew/bin/docker-compose -f docker-compose.yml config`
- `/opt/homebrew/bin/docker-compose -f docker-compose.yml up -d --build postgres redis api dashboard scheduler-worker market-stream-worker reconciliation-worker trade-monitor-worker review-worker learning-worker`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps api alembic -c trading_system/alembic.ini upgrade head`
- `curl -fsS http://localhost:8000/health`
- `curl -fsS http://localhost:8501/`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps scheduler-worker python -m trading_system.app.services.worker scheduler --once`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps market-stream-worker python -m trading_system.app.services.worker market-stream --once`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps reconciliation-worker python -m trading_system.app.services.worker reconciliation --once`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps trade-monitor-worker python -m trading_system.app.services.worker trade-monitor --once`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps review-worker python -m trading_system.app.services.worker review --once`
- `/opt/homebrew/bin/docker-compose run --rm --no-deps learning-worker python -m trading_system.app.services.worker learning --once`

## Missing local dependencies

- terraform

## Exact next steps for AWS validation

1. Install and configure AWS CLI credentials for the target account/region (default us-east-1).
2. Review infra/aws/main.tf and set required variables (VPC CIDR, secrets ARNs, image tags).
3. Run `terraform -chdir=infra/aws init` and `terraform -chdir=infra/aws plan` before any apply.
4. Build and push the application image to the ECR repository declared in Terraform.
5. Run the Alembic migration ECS task against the RDS endpoint (see .github/workflows/ci-cd.yml).
6. Set runtime secrets in AWS Secrets Manager: DATABASE_URL, ADMIN_PASSWORD, ADMIN_SESSION_SECRET, ALPACA_PAPER_*.
7. Deploy ECS services in order: api, dashboard, scheduler, market_stream, reconciliation, trade_monitor, reviews, learning.
8. Verify ALB /health on the API service and authenticated /ops/health after deployment.
9. Confirm ENVIRONMENT_MODE=paper, ALLOW_LIVE_TRADING=false, ENABLE_LIVE_ORDER_PATH=false in task definitions.
10. Generate a live-readiness report only after all provider health and admin-secret gates pass in the target environment.
