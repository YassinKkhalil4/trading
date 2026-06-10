---
name: Market-aware news scheduling
description: Why/where Alpha Vantage news pulls are gated by market session instead of a fixed cadence, and how to tune them.
---

# Market-aware news scheduling

News collection is NOT a fixed-cadence job like the other scheduler jobs. It is
gated by the current US market session via `news_pull_due(now, last_run, settings)`
(pure function, unit-tested independently):

- **Premarket** → one pull per trading day (deduped by comparing the last run's
  *Eastern* date to the session date). Toggle with `SCHEDULER_NEWS_PREMARKET`.
- **Regular / early-close session** → `SCHEDULER_NEWS_INTRADAY_PULLS` pulls spread
  evenly across the session; interval = session_length / pulls (so early-close days
  compress the same count into a shorter window).
- **After-hours / overnight / weekend / holiday** → paused.

**Why:** Alpha Vantage is rate-limited (free tier ~25 req/day) and news is fetched
**per symbol** (one API call per active symbol per pull), so the daily request
budget = pulls × universe size. Spreading pulls and pausing off-hours spends the
budget when news actually moves the market. Tune the two env vars to fit the key's
limit and the universe size.

**How to apply:** The gate lives at the *scheduling layer* only — both the
`run_once("all")` fan-out and `run_forever` special-case the `"news"` job through
`news_pull_due`. It is deliberately NOT enforced inside `run_once("news")`, so an
explicit/manual news run (or a test) still works at any time. If you add a new
scheduler driver, route news through `news_pull_due`, not a cadence. `"news"` must
stay in `_job_cadences` because `run_forever` iterates that dict to find the job.
`run_forever`'s sleep is capped at 60s so session transitions are caught promptly,
and its `next_due` min() skips jobs not yet in `last_run` (news may never have run).
