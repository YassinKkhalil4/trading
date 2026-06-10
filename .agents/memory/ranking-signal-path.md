---
name: Ranking signal path enablement
description: Why the opportunity-ranking signal path is gated, and the non-obvious prerequisite that makes it a no-op when enabled naively.
---

# Opportunity-ranking signal path

The live VWAP scan can route accepted scanner results through the
opportunity-ranking bridge (only A/A+ grades create signals) when the
`ENABLE_RANKING_SIGNAL_PATH` config flag is ON. Default is OFF.

## The gotcha
The live scan consumes `yahoo_chart` candles, but `build_preflight_payload`
hardcodes `provider="alpaca_market_data"` (PRODUCTION_DATA_PROVIDER), and the
ranking hard-block requires BOTH a fresh Alpaca provider-health snapshot AND a
fresh market-regime snapshot. So with the flag ON but the provider-health and
regime schedulers idle, the scan accepts candidates but **never** creates a
signal — and the stored preflight misattributes the data source.

**Why:** the ranking engine was originally designed around an Alpaca data path;
wiring it into the yahoo-fed live scan inherited that provider assumption.

**How to apply:** before enabling the flag, ensure the provider-health and
regime schedulers run (so fresh snapshots exist), or reconcile the preflight
provider with the source actually scanned. When testing the flag-ON path,
seed a fresh PRODUCTION_DATA_PROVIDER health snapshot + a fresh regime snapshot
and approve the DB strategy rows, or the hard-block trips first.
