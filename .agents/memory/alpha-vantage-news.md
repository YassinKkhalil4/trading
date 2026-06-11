---
name: Alpha Vantage NEWS_SENTIMENT gotchas
description: Non-obvious API semantics and security pitfalls when collecting news from Alpha Vantage's NEWS_SENTIMENT endpoint.
---

# Alpha Vantage NEWS_SENTIMENT

The trading platform's news collector (provider name kept as `"news"`, not
`"alpha_vantage"`, so downstream catalyst/scanner consumers stay unchanged)
pulls from Alpha Vantage's `NEWS_SENTIMENT` endpoint.

## Chosen design: ONE broad no-tickers feed, fanned out per ticker_sentiment
For a ~13k-symbol universe on the free tier, do NOT pass `tickers` at all. Issue
a single broad call (`function=NEWS_SENTIMENT`, `sort=LATEST`, `limit` up to
1000, no `tickers` param). Each returned article carries a `ticker_sentiment`
array; fan out **one CleanNews row per ticker_sentiment entry whose symbol is in
the preloaded active set** (per-symbol dedup via `seen_by_symbol`). One raw row
per article; clean rows fan out per matched ticker.

**Why:** one request returns up to 1000 latest market-wide articles regardless of
`limit`, so coverage scales without spending extra requests. The free tier is
~25 req/day; a broad pull is ~1 request, so cadence (a few pulls/day, market-
session gated) easily fits the budget.

**`limit` does not cost requests** — raising `ALPHA_VANTAGE_NEWS_LIMIT` (and its
config default) toward 1000 only enlarges the single response; it does NOT add
API calls. A low default (e.g. 50) silently starves coverage. Keep the default
high (1000) in BOTH the dataclass default and the `get_settings()` env fallback
(see config-default-drift memory).

## Multiple tickers is an AND filter — never batch symbols into `tickers`
If you ever set `tickers=A,B,C` it returns only articles that **simultaneously
mention all** of A, B and C (AND, not OR), not a batched per-symbol fetch — it
yields a near-empty feed while reporting success. The broad no-tickers feed above
is the correct approach; never "optimize" into a multi-ticker `tickers=` batch,
and don't revert to a one-request-per-symbol loop (burns the daily budget).

## Rate-limit / error responses are HTTP 200 with a message key
On throttle or bad params AV returns 200 with `Note`, `Information`, or
`Error Message` and no `feed`. Treat a missing/invalid `feed` list as a failure,
not an empty success.

## API key leaks via exception strings
`requests` exception messages embed the full request URL including
`apikey=<secret>`. Anything derived from an exception is persisted (ApiCallLog
`reason`, scheduler-run payload, dashboard). **Always redact** the key (strip
`apikey=...` and replace the literal key with `***`) before logging. The base
endpoint URL logged separately is fine because query params are passed via
`params=` and not part of the logged endpoint string.

## Raw-news growth (known, non-blocking)
The collector writes one RawNews row per article with no URL/hash dedup, so the
same article re-pulled across cadences (it stays in the LATEST window) creates
duplicate raw rows daily. CleanNews is safe — it dedups on
(normalized_headline_hash, symbol). If raw-row growth matters, dedup raw storage
by URL/hash before re-inserting.

## Timestamps
`time_published` format is `%Y%m%dT%H%M%S` (e.g. `20260603T143000`), no zone —
parse and attach UTC.
