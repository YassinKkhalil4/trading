# Agent memory

Keep this index limited to durable implementation caveats that are still useful
alongside the FastAPI router/service structure. Delete entries instead of
preserving stale route or scheduler narratives.

- [Settings defaults in two places](config-default-drift.md) — config.py defaults live in the dataclass AND get_settings() env-fallbacks; prod reads the fallbacks, so edit both.
- [unsafe_allow_html + user fields](streamlit-unsafe-html.md) — admin usernames are unvalidated; html.escape() any user-controlled value rendered via unsafe_allow_html (stored XSS).
- [Scheduler deployed path](scheduler-deployed-path.md) — the deployed path is `run_once("all")`, not `run_forever`; put cadence logic where it actually runs.
- [Alpha Vantage NEWS_SENTIMENT gotchas](alpha-vantage-news.md) — non-obvious API semantics and security pitfalls collecting news from NEWS_SENTIMENT.
