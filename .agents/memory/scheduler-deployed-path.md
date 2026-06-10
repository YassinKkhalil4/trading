---
name: Scheduler deployed path
description: Which scheduler code path actually runs in production, so scheduling/cadence logic is placed where it takes effect.
---

# Scheduler: the deployed path is `run_once("all")`, not `run_forever`

The production scheduler runs via the worker process: `worker.py` "scheduler" branch
→ `runtime.run_scheduled_job("all")` → `ScheduledCollectorRunner.run_once("all")`,
inside the worker's `while True` loop that sleeps `worker_sleep_seconds` (~5s) and
writes heartbeats. The `"all"` branch fans out to all child jobs each cycle.

`ScheduledCollectorRunner.run_forever()` exists but is **not invoked** by the worker.
Putting scheduling/cadence logic only in `run_forever` is a no-op in production.

**Why:** A cadence fix was first written in `run_forever` and silently had no effect —
low-frequency jobs (news/SEC) still fired every worker cycle because the live path is
`run_once("all")`.

**How to apply:** Any change to *when* jobs run (cadence, gating, throttling) must live
in (or be reachable from) the `run_once("all")` branch. Because `run_once("all")` is
called fresh each cycle, cross-cycle state (e.g. per-job cadence) must be derived from
persisted data — last-run times come from `repository.last_scheduler_run_times()`
(max `finished_at` per `job_name` in `scheduler_runs`). Do NOT switch the worker to call
`run_forever`; it would bypass the heartbeat/`--once` lifecycle.
