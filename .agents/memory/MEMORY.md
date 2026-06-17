# Agent memory

1. Project Overview
Goal: Autonomous, institutional-grade, asynchronous trading engine.
Core Stack: FastAPI, SQLAlchemy (PostgreSQL), Redis (for orchestration/locks), Alpaca (Broker), XGBoost (Alpha inference).
Status: High-maturity infrastructure; Low-maturity signal generation (Strategy logic currently heuristic-based).

2. Architectural State (Current)
Decoupling Goal: Actively liquidating TradingRuntimeService and runtime.py. All business logic is being migrated to domain-specific standalone orchestrators.

Primary Orchestrators:

DataPipelineOrchestrator: Ingestion, quality repair, and market calendar management.

ExecutionOrchestrator: Order submission, TWAP slicing, and reconciliation.

ResearchOrchestrator: Backtesting and performance attribution.

RiskAndSyncOrchestrator: Kill switches, live gates, and broker sync.

Safety Features:

Distributed Locking: Utilized in order submission and sensitive state updates to prevent race conditions.

Idempotency Keys: Required for all order submissions to prevent duplicate execution during network retries.

Live Gate Service: Hard gate preventing order execution if infrastructure health (data streams, kill switches, provider health) is not verified.

3. Active Rules & Directives (Enforced)
Orchestrator Isolation: No inheritance between orchestrators.

No Franken-Code: No monkeypatching or dynamic module mutation.

Preservation of Safety: Context managers (e.g., DistributedLock) must never be stripped during refactoring.

Destructive Refactoring: When migrating logic, physically extract code (Cut/Paste) rather than re-implementing.

4. Recent Refactors & Milestones
2026-06: Migrated logs to JSONB for schema efficiency; purged legacy AI models; flattened DB schema.

2026-06: Implemented global rate-throttling for SEC EDGAR data.

2026-06: Transitioned UI from Streamlit to React (Zustand state management).

5. Known Technical Debt / To-Do
Missing Signal Intelligence: Heuristic scanners (vwap_reclaim, etc.) are functional, but true signal generation (ML-driven) is not integrated.

UI/Backend Gap: Frontend state stores lack a concrete WebSocket bridge to backend operations.

Secrets Management: Environment-based secrets (alpaca_live_api_key) need to be migrated to a proper Vault provider.

Test Coverage: Infrastructure is tested; strategy effectiveness is un-tested.

6. Update Log (Append entries here)
Date: 2026-06-17 - Initialized MEMORY.md to establish the Memory Protocol for the platform.
