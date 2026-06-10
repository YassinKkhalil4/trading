---
name: Settings defaults live in two places
description: config.py has two independent sources of defaults that silently drift; production reads the env-fallback one.
---

# Settings defaults exist in two places — keep them in sync

`trading_system/app/core/config.py` defines defaults twice:
1. The `Settings` dataclass field defaults.
2. `get_settings()`, which constructs `Settings(...)` explicitly with `_env_float(ENV_VAR, FALLBACK)` per field.

**Why this bites:** Production and the dashboard/API all go through `get_settings()`, so the
*env-fallback* numbers (source #2) are what actually take effect when no env var is set — NOT the
dataclass defaults. Unit tests that build `Settings(...)` directly exercise source #1 and will pass
even when source #2 is stale. A re-weighting that only edits the dataclass defaults is a silent no-op
in production.

**How to apply:** Any change to a ranking/config default must edit BOTH the dataclass default and
the matching `_env_float`/`_env_int`/`_env_bool` fallback in `get_settings()`. A drift-guard test
(`test_get_settings_ranking_defaults_match_dataclass` in `test_config.py`) asserts the two match for
the ranking fields after clearing env vars + `get_settings.cache_clear()`; extend it if you add new
tunable config groups.
