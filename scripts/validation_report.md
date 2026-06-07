# Platform Validation Report

- Report version: 1.0.0
- Started: 2026-06-07T15:39:07.779373+00:00
- Finished: 2026-06-07T15:41:20.651188+00:00
- Root: `/Users/yassinkhalil/Documents/Trading`

## Passed checks

- **Python compileall** — PASS
  - trading_system package compiles cleanly
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m compileall -q trading_system`
- **Full pytest suite** — PASS
  - 235 passed, 1 warning in 12.95s
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m pytest trading_system/tests -q --tb=no`
- **Alembic upgrade head (clean PostgreSQL)** — PASS
  - Database trading_validation_1780846762 migrated to head
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m alembic -c trading_system/alembic.ini upgrade head`
- **Seed data loads** — PASS
  - {'symbols': 5, 'raw_ingestion_events': 0, 'raw_trade_ticks': 0, 'clean_candles': 0, 'stream_events': 0, 'provider_health': 0, 'provider_rate_limits': 0, 'worker_heartbeats': 0, 'clean_news': 0, 'filings': 0, 'scanner_results': 0, 'signals': 0, 'risk_checks': 0, 'broker_account_snapshots': 0, 'orders': 0, 'fills': 0, 'positions': 0, 'execution_errors': 0, 'journal_entries': 0, 'scheduler_runs': 0, 'live_readiness_reports': 0, 'strategy_approval_requests': 0, 'kill_switches': 0, 'weekly_reviews': 0, 'recommendations': 0}
- **Strategy registry has 7 strategies** — PASS
  - 7
- **Provider capabilities seeded (8)** — PASS
  - 8
- **Admin bootstrap with configured password** — PASS
  - admin=True login=True
- **Admin bootstrap rejects default unsafe session secret** — PASS
  - default=False
- **research starts without broker execution** — PASS
  - Paper execution requires ENVIRONMENT_MODE=paper.
- **paper starts with Alpaca paper config only** — PASS
  - live_order_path_enabled=False in paper mode
- **paper adapter requires paper credentials** — PASS
  - configured only when paper keys present
- **live_disabled blocks live order path** — PASS
  - environment_mode_live, allow_live_trading, confirm_live_trading, live_order_path_enabled, live_keys_present, active_human_approval, latest_readiness_passed, alpaca_market_data_healthy, alpaca_live_healthy, live_account_snapshot_usable, live_reconciliation_clean, strategy_approved
- **live remains blocked without every gate** — PASS
  - ENVIRONMENT_MODE=live requires explicit live confirmation. Live trading is not wired unless every live gate is explicitly enabled.
- **worker scheduler imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker scheduler --once`
- **worker market-stream imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker market-stream --once`
- **worker reconciliation imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker reconciliation --once`
- **worker trade-monitor imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker trade-monitor --once`
- **worker review imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker review --once`
- **worker learning imports and starts** — PASS
  - completed one cycle
  - Command: `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker learning --once`
- **dashboard/API show no fake positions/orders/fills on fresh bootstrap** — PASS
  - {'symbols': 5, 'raw_ingestion_events': 0, 'raw_trade_ticks': 0, 'clean_candles': 0, 'stream_events': 0, 'provider_health': 0, 'provider_rate_limits': 0, 'worker_heartbeats': 0, 'clean_news': 0, 'filings': 0, 'scanner_results': 0, 'signals': 0, 'risk_checks': 0, 'broker_account_snapshots': 0, 'orders': 0, 'fills': 0, 'positions': 0, 'execution_errors': 0, 'journal_entries': 0, 'scheduler_runs': 0, 'live_readiness_reports': 0, 'strategy_approval_requests': 0, 'kill_switches': 0, 'weekly_reviews': 0, 'recommendations': 0}
- **missing paper credentials show unconfigured status** — PASS
  - Alpaca paper keys are not configured.
- **missing provider credentials show blocked/unconfigured health** — PASS
  - 8 non-healthy snapshots
- **API /health advertises gated live path** — PASS
  - Production-gated platform. Live trading path is disabled unless every live gate passes.

## Failed checks

_None._

## Skipped checks

- **docker available** — SKIP
  - docker not installed on this host
- **terraform available** — SKIP
  - terraform not installed on this host
- **Docker image build (API)** — SKIP
  - Docker not installed
- **Docker image build (dashboard)** — SKIP
  - Docker not installed
- **docker-compose config validation** — SKIP
  - Docker not installed
- **docker-compose startup health check** — SKIP
  - Docker not installed

## Commands run

- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m compileall -q trading_system`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m pytest trading_system/tests -q --tb=no`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m alembic -c trading_system/alembic.ini upgrade head`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker scheduler --once`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker market-stream --once`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker reconciliation --once`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker trade-monitor --once`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker review --once`
- `/Users/yassinkhalil/Documents/Trading/.venv/bin/python -m trading_system.app.services.worker learning --once`

## Missing local dependencies

- docker
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
