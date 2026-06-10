# Local Docker Paper Trading

Run the platform in **paper mode** with Alpaca paper credentials. Live execution stays disabled.

## Prerequisites

- Docker Desktop or Colima (daemon running)
- Alpaca **paper** API key and secret ([Alpaca dashboard](https://app.alpaca.markets/paper/dashboard/overview))
- Do **not** set live keys unless you are explicitly testing live in a separate environment

## 1. Create `.env` from `.env.example`

```bash
cp .env.example .env
```

Edit `.env` with at least:

| Variable | Paper Docker value |
|----------|-------------------|
| `ENVIRONMENT_MODE` | `paper` |
| `ALLOW_LIVE_TRADING` | `false` |
| `ENABLE_LIVE_ORDER_PATH` | `false` |
| `DEPLOYMENT_TARGET` | `docker` |
| `DATABASE_URL` | Leave unset in `.env` — `docker-compose.yml` sets PostgreSQL for containers |
| `REDIS_URL` | `redis://redis:6379/0` (or rely on compose defaults inside workers) |
| `ALPACA_PAPER_API_KEY` | Your paper key |
| `ALPACA_PAPER_SECRET_KEY` | Your paper secret |
| `ADMIN_PASSWORD` | Strong local admin password |
| `ADMIN_SESSION_SECRET` | High-entropy secret (not `change-me`) |
| `API_ADMIN_TOKEN` | Optional API admin token |

### Paper-only Alpaca settings

```bash
ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER_DATA_URL=https://data.alpaca.markets
ALPACA_PRIMARY_DATA_FEED=iex
```

### Live keys — keep empty

```bash
# ALPACA_LIVE_API_KEY=
# ALPACA_LIVE_SECRET_KEY=
```

Never commit `.env` (it is gitignored).

## 2. Start the stack

For a **clean** database (no prior orders/fills/positions):

```bash
docker compose down -v
```

Then start services:

```bash
docker compose config
docker compose up -d --build
```

Services:

- `postgres`, `redis`
- `api`, `dashboard`
- `scheduler-worker`, `market-stream-worker`, `reconciliation-worker`
- `trade-monitor-worker`, `review-worker`, `learning-worker`

## 3. Run migrations

```bash
docker compose run --rm --no-deps api alembic -c trading_system/alembic.ini upgrade head
```

## 4. Smoke test (readiness)

Default checks only (no broker orders):

```bash
.venv/bin/python scripts/docker_paper_smoke_test.py
```

Optional tiny **paper** test order (explicit opt-in):

```bash
.venv/bin/python scripts/docker_paper_smoke_test.py --submit-paper-test-order
```

The flag is disabled by default. It only runs when `ENVIRONMENT_MODE=paper`, verifies the live path is blocked first, submits a minimal market order to Alpaca paper, then syncs and reconciles.

## 5. Manual health checks

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8501/
```

Worker one-shot cycles:

```bash
docker compose run --rm --no-deps scheduler-worker \
  python -m trading_system.app.services.worker scheduler --once
```

Repeat for `market-stream`, `reconciliation`, `trade-monitor`, `review`, `learning`.

## Safety defaults

The stack must keep:

- `ENVIRONMENT_MODE=paper`
- `ALLOW_LIVE_TRADING=false`
- `ENABLE_LIVE_ORDER_PATH=false`
- Live Alpaca keys unset

Paper orders go only to Alpaca's paper API when you explicitly pass `--submit-paper-test-order` in the smoke script.
