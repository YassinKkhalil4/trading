# Autonomous Trading Intelligence Platform

This is a production-gated trading intelligence platform. It is live-capable in code, but live execution is disabled unless every explicit live gate passes.

The platform now supports:

1. Collect and validate market data.
2. Store raw data before cleaned data.
3. Compute features.
4. Scan for VWAP Reclaim, Post-Earnings Continuation, Opening Range Breakout, News Momentum, Catalyst Run-Up, Relative Strength, and Sector Leadership candidates behind production preflight gates.
5. Backtest with explicit slippage, commission, spread, and entry-delay assumptions.
6. Journal trades from entry through exit with PnL, MFE, MAE, slippage, time in trade, decision-support reviews, and rule violations.
7. Paper trade through Alpaca.
8. Evaluate live readiness, kill switches, provider health, and broker reconciliation before any live order path can run.

## Safety Model

- `ENVIRONMENT_MODE` supports `research`, `paper`, `live_disabled`, and `live`.
- Default mode is `research`.
- Live mode is never the default.
- Live order submission requires `ENVIRONMENT_MODE=live`, `ALLOW_LIVE_TRADING=true`, `CONFIRM_LIVE_TRADING=I_UNDERSTAND_RISK`, `ENABLE_LIVE_ORDER_PATH=true`, live keys, active human approval, healthy providers, clean live reconciliation, approved strategies, and no active kill switch.
- Admin user management is API-backed, bcrypt-hashed, role-gated, redacted on read, and audited for create/update, role, active-state, and unlock actions.
- Alpaca paper trading is treated as simulated execution, not proof of live fill quality.
- Decision support may score, explain, review, and recommend, but cannot trade, override risk, change rules, or bypass live gates.
- Learning recommendations are audited, stored as proposed-only recommendations, and cannot auto-apply changes.
- Public health exposes only liveness. Detailed API read surfaces such as operational health, environment state, provider capabilities, strategy approvals, dashboard snapshots, and worker status require authenticated viewer-or-higher access.

## Quick Start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Run API:

```bash
uvicorn trading_system.app.api.main:app --reload
```

Run dashboard:

```bash
streamlit run trading_system/dashboard/app.py
```

The dashboard is database-backed. It shows only real provider calls and persisted decisions:

- market candles, features, scanner decisions, signals, and theses
- Alpaca market-data stream events
- paper orders, fills, broker sync logs, and reconciliation results
- SEC filings and news catalyst rows
- scheduler runs, audit logs, decision logs, and live-readiness reports
- journal lifecycle metrics, decision-support reviews, weekly reviews, learning recommendations, provider artifacts, and scorecard evaluations
- provider health, data quality, missing-candle gaps, backtest reports, live approvals, and kill switches

Run tests:

```bash
pytest
```

## Database

Set `DATABASE_URL` in `.env`, then run:

```bash
alembic -c trading_system/alembic.ini upgrade head
```

The initial migration creates the schema from SQLAlchemy metadata for this greenfield platform.
Future migrations should be explicit column/table migrations.
Production deployment runs Alembic migrations as a one-off task before service rollout; application startup does not auto-create production schema.

## Important Provider Rules

- Alpaca paper/data: primary paper execution, market data, account sync, orders, fills, and positions if credentials permit.
- Alpaca live: implemented as a separate adapter, but blocked unless every live gate passes.
- Yahoo/yfinance-style data: research/prototyping only. Scheduler and primary collection fall back to Yahoo only in `research` mode, never in paper/live-disabled/live workflows.
- Alpha Vantage: enrichment only, not heavy intraday collection.
- SEC EDGAR: slow filings/fundamentals/catalyst context only.
- News providers: enrichment/catalyst context only.

## Operational Endpoints

- `POST /streams/alpaca/market-data/run-once` runs a bounded Alpaca websocket sample and stores stream events/candles.
- `POST /reconciliation/fills/run-once` syncs Alpaca paper orders/positions and records fills.
- `POST /collect/news` fetches configured RSS feeds and stores raw/clean news with confidence, duplicate, and rumor flags.
- `POST /collect/sec` fetches SEC company submissions and stores filing context.
- `POST /scheduler/run-once` runs one scheduled job: `market_data`, `features`, `regime`, `news`, `sec`, `catalysts`, `production_scanners`, `provider_health`, `universe`, `missing_candle_repair`, `live_readiness`, `fill_reconciliation`, `trade_monitor`, `reviews`, `learning`, or `all`.
- `GET /decision-support/status`, `GET /decision-support/artifacts`, `GET /scorecards/opportunities`, and `GET /scorecards/evaluations` expose the read-only decision-support audit trail for authenticated users.
- `POST /scorecards/evaluations/run` recalculates scorecard calibration reports from persisted scorecards and journal outcomes; it does not submit trades or mutate strategy rules.
- `POST /live-readiness/report` stores a live-readiness report. It does not bypass live gates.
- `POST /execution/live/submit`, `POST /execution/live/cancel-all`, and `POST /execution/live/flatten-all` exist for live operations but return blocked responses unless live mode, live path, keys, readiness, approval, provider health, reconciliation, strategy approval, risk, and kill-switch gates pass.

## Production Topology

AWS Terraform deploys independent ECS Fargate services for API, dashboard, scheduler, market stream, reconciliation, trade monitor, reviews, and learning. PostgreSQL is the source of truth, Redis is used for cache/coordination, and raw provider payloads can be archived to S3 when `RAW_ARCHIVE_BUCKET` is configured.

## Backtest Warning

Early S&P 500 backtests are marked as potentially survivorship-biased unless a point-in-time universe is used.
