---
name: Alpha Vantage NEWS_SENTIMENT gotchas
description: Non-obvious API semantics and security pitfalls when collecting news from Alpha Vantage's NEWS_SENTIMENT endpoint.
---

# Alpha Vantage NEWS_SENTIMENT

The trading platform's news collector (provider name kept as `"news"`, not
`"alpha_vantage"`, so downstream catalyst/scanner consumers stay unchanged)
pulls from Alpha Vantage's `NEWS_SENTIMENT` endpoint.

## Multiple tickers is an AND filter (not OR)
`tickers=A,B,C` returns only articles that **simultaneously mention all** of A,
B and C — it is NOT a batched per-symbol fetch. Batching many symbols into one
call therefore returns an empty/near-empty feed in production while still
reporting success, silently producing no news.

**How to apply:** issue one request per symbol (mirrors the old RSS per-symbol
loop). Accept the free-tier rate-limit trade-off (≈25 req/day) — tune via
scheduler cadence (`SCHEDULER_NEWS_SECONDS`), universe size, and
`ALPHA_VANTAGE_NEWS_LIMIT` (articles per call). Do not "optimize" back into a
multi-ticker batched call.

## Rate-limit / error responses are HTTP 200 with a message key
On throttle or bad params AV returns 200 with `Note`, `Information`, or
`Error Message` and no `feed`. Treat a missing/invalid `feed` list as a failure
for that symbol, not an empty success.

## API key leaks via exception strings
`requests` exception messages embed the full request URL including
`apikey=<secret>`. Anything derived from an exception is persisted (ApiCallLog
`reason`, scheduler-run payload, dashboard). **Always redact** the key (strip
`apikey=...` and replace the literal key with `***`) before logging. The base
endpoint URL logged separately is fine because query params are passed via
`params=` and not part of the logged endpoint string.

## Timestamps
`time_published` format is `%Y%m%dT%H%M%S` (e.g. `20260603T143000`), no zone —
parse and attach UTC.
