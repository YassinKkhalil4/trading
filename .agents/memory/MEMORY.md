# Agent memory

# 🧠 SYSTEM MEMORY & ARCHITECTURAL STATE

**Last Updated:** June 17, 2026  
**Primary Mandate:** Institutional-grade structural integrity, strict domain isolation, and production safety.

---

## 🏛️ 1. CURRENT ARCHITECTURAL STATE
The system is a highly complex, asynchronous execution framework masquerading as an autonomous platform. It possesses institutional-grade infrastructure for data ingestion and risk gating, but currently lacks functional, autonomous trading signal logic (the "intelligence" layer is primarily mocked or relies on hardcoded heuristic thresholds).

**Core Stack:** FastAPI, PostgreSQL (heavy reliance, massive schema), Redis (orchestration locks/rate throttling), Alpaca API (Live & Paper).
**Design Philosophy:** Headless, autonomous engine. The frontend (Next.js/React) is purely cosmetic and disconnected from the core state loop. 

**Domain Separation (In Progress):**
The system is undergoing a massive structural refactor to break down the monolithic `TradingRuntimeService` (`runtime.py`) into isolated, standalone orchestrators located in `trading_system/app/services/orchestrators/`:
* `DataPipelineOrchestrator.py`
* `ExecutionOrchestrator.py`
* `RiskAndSyncOrchestrator.py`
* `ResearchOrchestrator.py`

**Strict Architectural Rules:**
1.  **NO INHERITANCE:** Orchestrators are independent standalone classes.
2.  **PHYSICAL EXTRACTION:** Code must be cut and pasted exactly as is from legacy services to new orchestrators. Do not rewrite business logic from scratch.
3.  **NO DYNAMIC MUTATION:** Monkeypatching to fix tests is strictly prohibited. Fix the decoupled architecture properly.

---

## 🛡️ 2. ACTIVE SAFETY MECHANISMS (CRITICAL - DO NOT BREAK)
Whenever extracting or refactoring code, the following mechanisms **MUST** physically survive the move unaltered:

1.  **Distributed Locks:** Redis-based locks (e.g., `with DistributedLock(...)`) prevent race conditions during high-frequency orchestrator loops. They must wrap all state-mutating market and execution operations.
2.  **Live Execution Gates (`live_gates.py`):** The `LiveGateService` blocks orders based on provider health, reconciliation status, and heartbeat timestamps. *Warning: Currently highly dependent on system logs/heartbeats (`alpaca_stream`); susceptible to stale data if streams lag.*
3.  **Idempotency & Order Submission (`live_execution.py` / `order_manager.py`):** The generation of `client_order_id` (timestamp-based) and TWAP slicing logic. Never abstract this into generic wrappers.
4.  **Kill Switches (`kill_switch.py`):** System-wide emergency halts. Must be accessible by all newly decoupled orchestrators.
5.  **Audit & Decision Logging:** Every system action, rejection, and decision is written to `AuditLog` and `DecisionLog`. This path must remain unbroken.

---

## 🚧 3. THE ROADMAP: THE DEATH OF THE GOD OBJECT
**Current Primary Objective:** Complete the eradication of `TradingRuntimeService`.

* **Status:** In Progress. Standalone orchestrators exist but dependencies are tangled.
* **Next Steps:**
    * Verify all properties, methods, and states have been physically extracted to their respective domain orchestrators.
    * Ensure all scheduled tasks (`tasks.py` / `scheduler.py`) point directly to the isolated orchestrators, not a central runtime manager.
    * Delete `runtime.py`.

---

## ⚠️ 4. KNOWN VULNERABILITIES & MISSING LINKS
* **The AI/Alpha Facade:** `ml_inference.py` loads an XGBoost model, but `strategies.py` relies on hardcoded scanner heuristics (e.g., VWAP, BPS spread). The actual execution manager does not map signals to live trading. 
* **Secret Management:** `live_readiness.py` currently checks for a hardcoded default `admin_session_secret == "change-me"`.
* **Live Data Lag:** The live path relies on the "HEALTHY" status of providers. If a stream is "HEALTHY" but lagging behind actual market time, the system will execute on stale data.
* **Cosmetic UI:** The React dashboard operates without a real-time websocket connection to the FastAPI backend state.

---

## 📝 5. AGENT ACTION LOG (CHANGELOG)
*(Agent MUST append exactly what was extracted, deleted, or altered at the end of every successful execution).*

* **[2026-06-17]**: Initialized `MEMORY.md`. Documented the requirement to eliminate `TradingRuntimeService` and explicitly identified active safety mechanisms (Locks, TWAP logic, Live Gates) that must be preserved during the extraction phase.
