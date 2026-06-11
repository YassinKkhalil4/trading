# Autonomous Trading Intelligence Platform

This is a production-gated trading intelligence platform. It is live-capable in code, but live execution is disabled unless every explicit live gate passes.

The platform now supports:

1. Collect and validate market data.
2. Store raw data before cleaned data.
3. Compute features.
4. Scan for VWAP Reclaim, Post-Earnings Continuation, Opening Range Breakout, News Momentum, Catalyst Run-Up, Relative Strength, and Sector Leadership candidates behind production preflight gates.
5. Score event-driven U.S. equity alpha opportunities with persisted 0-100 scores, component breakdowns, penalties, grades, expected R, confidence, and suggested paper-mode risk multipliers.
6. Persist point-in-time universe snapshots so research/backtests can avoid survivorship-biased current-constituent lookups.
7. Persist short-interest context (short % of float, days-to-cover, borrow fee, utilization, and float) for squeeze/reversal setups.
8. Persist options-intelligence context (IV rank/percentile, open interest, gamma/delta exposure, expected move, and weekly/earnings expiry flags).
9. Rank sector leadership using actual sector ETF vs SPY analytics when ETF candles are available, with member-inferred fallback.
10. Score separate multi-bagger/long-shot candidates for 3x/5x/10x watchlists using narrative, growth, capital flows, accumulation, squeeze, and options-leverage components.
11. Backtest with explicit slippage, commission, spread, and entry-delay assumptions.
12. Journal trades from entry through exit with PnL, MFE, MAE, slippage, time in trade, reviews, and rule violations.
13. Paper trade through Alpaca.
14. Evaluate live readiness, kill switches, provider health, and broker reconciliation before any live order path can run.

## Safety Model

- `ENVIRONMENT_MODE` supports `research`, `paper`, `live_disabled`, and `live`.
- Default mode is `research`.
- Live mode is never the default.
- Live order submission requires `ENVIRONMENT_MODE=live`, `ALLOW_LIVE_TRADING=true`, `CONFIRM_LIVE_TRADING=I_UNDERSTAND_RISK`, `ENABLE_LIVE_ORDER_PATH=true`, live keys, active human approval, healthy providers, clean live reconciliation, approved strategies, and no active kill switch.
- Admin user management is API-backed, bcrypt-hashed, role-gated, redacted on read, and audited for create/update, role, active-state, and unlock actions.
- Alpaca paper trading is treated as simulated execution, not proof of live fill quality.
- AI may score and explain, but cannot trade, override risk, or change rules.
- Alpha opportunity score can only reduce or modulate paper/live-disabled sizing after hard risk gates; it cannot bypass risk limits, strategy approval, live readiness, broker reconciliation, provider health, or kill switches.
- Multi-bagger scoring is a watchlist/research layer, not an intraday execution signal.
- Learning recommendations are audited, stored as recommendations, and cannot auto-apply changes.
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
- journal lifecycle metrics, AI reviews, weekly reviews, and learning recommendations
- provider health, data quality, missing-candle gaps, backtest reports, live approvals, and kill switches
- Alpha Command Center data: opportunity scores, rejection reasons, expectancy snapshots, sector/stock leadership, point-in-time universe rows, short-interest snapshots, options-intelligence snapshots, and multi-bagger watchlist scores

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

## Alpha Layer

The alpha layer is event-driven by design. It prioritizes catalyst quality and freshness, volume confirmation, price reaction, VWAP/ORB/reclaim structure, relative strength versus SPY and sector, liquidity/spread quality, market regime, historical expectancy, and adaptive paper-mode risk sizing. It intentionally avoids generic RSI/MACD crossover strategies.

Persisted alpha intelligence tables include:

- `opportunity_scores` and `opportunity_score_components` for explainable 0-100 opportunity grades.
- `alpha_rejection_reasons` for auditable rejected candidates.
- `expectancy_snapshots` and `strategy_performance_buckets` for historical performance by strategy/setup/context buckets.
- `sector_strength_snapshots` and `symbol_relative_strength_snapshots` for sector/stock leadership.
- `point_in_time_universe_memberships` for point-in-time research universes.
- `short_interest_snapshots` for short squeeze/reversal context.
- `options_intelligence_snapshots` for weekly/earnings options context.
- `multi_bagger_candidate_scores` for long-shot 3x/5x/10x watchlists.

## Operational Endpoints

- `POST /streams/alpaca/market-data/run-once` runs a bounded Alpaca websocket sample and stores stream events/candles.
- `POST /reconciliation/fills/run-once` syncs Alpaca paper orders/positions and records fills.
- `POST /collect/news` fetches configured RSS feeds and stores raw/clean news with confidence, duplicate, and rumor flags.
- `POST /collect/sec` fetches SEC company submissions and stores filing context.
- `POST /scheduler/run-once` runs one scheduled job: `market_data`, `features`, `regime`, `news`, `sec`, `catalysts`, `production_scanners`, `provider_health`, `universe`, `missing_candle_repair`, `live_readiness`, `fill_reconciliation`, `trade_monitor`, `reviews`, `learning`, or `all`.
- `POST /live-readiness/report` stores a live-readiness report. It does not bypass live gates.
- `GET /alpha/opportunity-scores`, `/alpha/candidates`, `/alpha/rejections`, `/alpha/expectancy`, and `/alpha/sector-leadership` expose alpha command-center data.
- `POST /alpha/scanners/run`, `/alpha/scoring/run-once`, `/alpha/expectancy/refresh`, and `/alpha/regime/refresh` run alpha scanners/scoring/refreshes manually.
- `GET /alpha/point-in-time-universe`, `/alpha/short-interest`, `/alpha/options-intelligence`, and `/alpha/multi-bagger-candidates` expose the new missing intelligence layers.
- `POST /alpha/point-in-time-universe/refresh`, `/alpha/short-interest/refresh`, `/alpha/options-intelligence/refresh`, and `/alpha/multi-bagger-candidates/score` refresh those layers from current persisted inputs.
- `POST /execution/live/submit`, `POST /execution/live/cancel-all`, and `POST /execution/live/flatten-all` exist for live operations but return blocked responses unless live mode, live path, keys, readiness, approval, provider health, reconciliation, strategy approval, risk, and kill-switch gates pass.

## Production Topology

AWS Terraform deploys independent ECS Fargate services for API, dashboard, scheduler, market stream, reconciliation, trade monitor, reviews, and learning. PostgreSQL is the source of truth, Redis is used for cache/coordination, and raw provider payloads can be archived to S3 when `RAW_ARCHIVE_BUCKET` is configured.

## Backtest Warning

Early S&P 500 backtests are marked as potentially survivorship-biased unless a point-in-time universe is used. The `point_in_time_universe_memberships` table now supports dated membership snapshots so future backtests can request the tradable universe as it existed on the test date rather than relying on today's surviving symbols.
