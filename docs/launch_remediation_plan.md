# Pre-Launch Remediation Plan

This plan converts the launch-readiness feedback into an ordered engineering backlog. The goal is to remove broken or misleading features, restore schema/API consistency, and replace fragile in-process infrastructure with production-grade components before live trading is enabled.

## Launch Blockers (must finish before any production launch)

### 1. Resolve schema drift and dead AI review models

**Problem:** The Alembic migration `trading_system/migrations/versions/cf5681ce63ec_purge_unused_ai_models_and_flatten_.py` drops obsolete AI tables, while `trading_system/app/db/models.py`, repository helpers, and admin endpoints still reference dropped tables. This can produce `ProgrammingError` failures when `/reviews/trades` or score endpoints query non-existent relations.

**Plan:**

1. Delete dead SQLAlchemy models from `trading_system/app/db/models.py`:
   - `TradeThesis`
   - `AIReview`
   - `MultiBaggerCandidateScore`
2. Remove or replace repository methods that create/list those records in `trading_system/app/db/repositories.py`.
3. Remove admin API routes backed by dropped tables from `trading_system/app/api/routers/admin.py`, including trade-review run/list endpoints.
4. Update or delete frontend calls/components that depend on those endpoints, replacing them with explicit "feature removed" empty states only if the UI still needs navigation compatibility.
5. Add a database integration test that creates a fresh database, applies all Alembic migrations, imports all SQLAlchemy models, and verifies every model table exists or is intentionally externalized.

**Acceptance criteria:**

- Fresh Alembic upgrade succeeds from an empty database.
- API tests prove removed endpoints return `404` or are absent from OpenAPI.
- No code references the dropped models.
- CI runs at least one migration-vs-model consistency check against PostgreSQL.

### 2. Fix production market-data fallback behavior

**Problem:** The system can fall back to Yahoo Finance when Alpaca fails, but the policy is stricter in research mode than production. In production, an Alpaca outage can leave the bot blind instead of degraded-but-aware.

**Plan:**

1. Centralize provider fallback policy in the provider router/service layer instead of scattering environment checks.
2. Allow production fallback only for read-only market-data enrichment, never for order execution or broker account truth.
3. Attach provenance metadata to every fallback quote/bar so downstream risk checks know whether a value came from Alpaca, Yahoo, cache, or stale storage.
4. Add freshness gates that fail closed when fallback data is stale, missing, delayed, or outside configured market hours.
5. Emit structured alerts whenever production fallback is used.

**Acceptance criteria:**

- Unit tests cover Alpaca failure with Yahoo fallback in production.
- Runtime logs/metrics include provider, freshness age, and fallback reason.
- Order placement remains blocked if execution-critical broker state cannot be verified from Alpaca.

### 3. Make price action monitoring active by default

**Problem:** `news_only_mode` defaults to `True`, which prevents the bot from monitoring price action unless explicitly overridden.

**Plan:**

1. Change the default `news_only_mode` setting to `False` in `trading_system/app/core/config.py`.
2. Update environment examples, tests, and documentation to state that news-only operation is an explicit opt-in mode.
3. Add startup/readiness output that clearly reports whether price monitoring is active.

**Acceptance criteria:**

- `Settings()` defaults to price-and-news monitoring.
- Tests assert the default behavior.
- Documentation explains how to opt into news-only mode for low-risk research runs.

### 4. Replace local paper execution with Alpaca Paper API

**Problem:** `PaperExecutionEngine` blindly records submitted orders and assumes perfect fills. This creates misleading test confidence and does not simulate matching, slippage, partial fills, latency, or rejects.

**Plan:**

1. Remove `PaperExecutionEngine` from runtime order submission paths.
2. Route paper-mode execution through the Alpaca Paper adapter.
3. Keep local-only order fixtures only for isolated unit tests, clearly named as fakes and never wired into runtime code.
4. Update scanner-to-execution integration tests to mock the broker adapter boundary rather than asserting local fake fills.
5. Add a paper smoke test that submits, polls, and cancels a small paper order against Alpaca Paper in a gated environment.

**Acceptance criteria:**

- Runtime code cannot submit paper orders through the local fake engine.
- Local fakes are test-only and named accordingly.
- Paper trading behavior reflects broker-side order states.

## High-Priority Architecture Remediation

### 5. Move scheduling to Celery Beat

**Problem:** `trading_system/app/services/scheduler.py` runs a custom loop with sleep intervals and Redis locks even though the application already imports Celery. This duplicates scheduler infrastructure and increases operational risk.

**Plan:**

1. Inventory all current scheduler cadences and lock keys.
2. Define Celery tasks for each recurring job with idempotent task bodies.
3. Configure Celery Beat schedules from settings or a dedicated beat configuration module.
4. Preserve distributed mutual exclusion around task bodies where needed, but remove the process-level `while True` loop.
5. Add operational docs for running worker and beat processes separately.

**Acceptance criteria:**

- Recurring jobs are scheduled by Celery Beat.
- No runtime entrypoint depends on an infinite scheduler loop.
- Tests validate task registration, cadence configuration, and lock behavior.

### 6. Keep V1 time-series storage on PostgreSQL with strict retention

**Problem:** `RawMarketData` and `RawTradeTick` are high-volume candle/tick tables. Moving them to TimescaleDB or InfluxDB before V1 would reduce one risk while adding larger launch risks: new operational infrastructure, backup/restore complexity, deployment changes, and unfamiliar query/maintenance paths.

**V1 plan:**

1. Keep raw candle/tick storage in standard PostgreSQL for V1.
2. Strictly enforce day- or week-based partitioning for `RawMarketData` and `RawTradeTick`.
3. Add a Celery-managed pruning job that deletes or drops tick partitions older than 7 days.
4. Keep compact aggregates and execution-critical metadata longer than raw tick retention windows.
5. Monitor ingestion volume, partition sizes, autovacuum behavior, replication lag, and WAL growth.

**Backlog trigger for TimescaleDB or a dedicated tick store:**

Only revisit a specialized time-series database after measured production-like ingestion shows PostgreSQL is the bottleneck, especially if daily ingestion actively overwhelms WAL throughput, retention pruning cannot keep table/index bloat controlled, or required analytics queries cannot meet latency targets on partitioned PostgreSQL.

**Acceptance criteria:**

- Raw tick/candle tables are partitioned by day or week.
- A Celery pruning task enforces a 7-day raw tick retention policy.
- Metrics and alerts cover table size, partition count, WAL growth, pruning failures, and ingestion latency.
- The TimescaleDB/InfluxDB migration remains a documented backlog option, not a V1 launch blocker.

### 7. Harden Alpaca stream freshness and reconnect handling

**Problem:** Stream reconnect settings exist, but silent disconnects or stale quotes can still make the bot trade on old market data if freshness checks are not applied consistently.

**Plan:**

1. Add heartbeat tracking per stream and per symbol.
2. Mark symbols unavailable when quote/bar age exceeds `bar_freshness_max_seconds` or stream heartbeat thresholds.
3. Make stale market data a hard pre-trade risk failure.
4. Add reconnect metrics, alerts, and circuit-breaker behavior after repeated failures.
5. Test silent disconnect, delayed data, and reconnect-after-gap scenarios.

**Acceptance criteria:**

- No trade can pass risk checks using stale quote/bar data.
- Stream health appears in readiness and alerting output.
- Tests simulate stale and disconnected stream states.

### 8. Keep admin controls locked down

**Problem:** Dangerous admin endpoints exist, including database bootstrap and user role mutation routes. The current live-mode secret guard is good, but non-live environments still need explicit defense-in-depth.

**Plan:**

1. Keep the existing live-mode failsafe that rejects default `ADMIN_SESSION_SECRET` and requires `CONFIRM_LIVE_TRADING`.
2. Add tests that dangerous admin endpoints reject default/weak secrets in every non-local deployment profile.
3. Require audit logging for role changes, bootstrap actions, and trading-control mutations, emitted as structured logs to stdout/stderr so Datadog, CloudWatch, or the container log pipeline can ingest them even if PostgreSQL is unavailable or compromised.
4. Add rate limits and short token expirations for admin sessions.
5. Document secure secret provisioning for staging and live deployments.

**Acceptance criteria:**

- Admin mutation routes require strong secrets and privileged principals.
- Security tests cover bootstrap and role-change routes.
- Audit events are emitted to stdout/stderr as structured logs with actor, action, timestamp, target resource, request ID, source IP, and outcome; optional database audit rows may exist only as a secondary convenience copy.

## Product and Strategy Clarifications

### 9. Make opportunity ranking honest and inspectable

**Problem:** Ranking weights such as `ranking_weight_regime` and `ranking_weight_catalyst` are hardcoded heuristics. They should not be presented as machine learning.

**Plan:**

1. Rename UI/API language from "AI score" or "ML ranking" to "heuristic opportunity score" wherever applicable.
2. Return score components in API responses so users can see how each score was computed.
3. Move default weights to documented configuration with range validation.
4. Add calibration metrics comparing scores to realized outcomes before treating ranking as a predictive model.
5. Only introduce ML ranking after a versioned training, validation, and monitoring pipeline exists.

**Acceptance criteria:**

- Product copy accurately describes the score as heuristic.
- API responses expose score explainability.
- Tests cover weight validation and score component accounting.

### 10. Define and test real alpha strategy inputs

**Problem:** The system has trade plumbing but expects external signals. The strategy layer must explicitly define how signals are produced, validated, and promoted to execution candidates.

**Plan:**

1. Document supported signal sources and their contracts.
2. Add at least one first-party strategy module with deterministic entry/exit logic, feature requirements, and risk constraints.
3. Require signal provenance, version, timestamp, and confidence metadata.
4. Backtest and paper-test the strategy before enabling live order routing.
5. Add integration tests from signal generation through risk approval and broker submission.

**Acceptance criteria:**

- The system can produce at least one internally generated strategy signal.
- Signals are versioned, reproducible, and auditable.
- Strategy-to-execution integration is covered in CI.

## Suggested Delivery Order

1. Schema drift cleanup and migration/model integration test.
2. Default config fix for `news_only_mode`.
3. Runtime removal of local paper execution.
4. Production market-data fallback hardening.
5. Celery Beat migration.
6. Stream freshness hardening.
7. PostgreSQL partitioning and 7-day raw tick pruning.
8. Admin security hardening.
9. Opportunity-ranking transparency.
10. First-party strategy/alpha implementation.

## Release Gates

Before launch, require all of the following:

- A clean PostgreSQL migration from empty database to head.
- A migration/model drift test in CI.
- Paper-mode execution through Alpaca Paper only.
- No runtime references to dropped AI tables.
- Price monitoring enabled by default.
- Stale market data blocks trading.
- Admin endpoints tested with strong-secret requirements.
- Raw tick/candle retention is enforced on partitioned PostgreSQL without requiring TimescaleDB or InfluxDB for V1.
- Documented rollback procedures for scheduler, execution, and data-provider failures.
