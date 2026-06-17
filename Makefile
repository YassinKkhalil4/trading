.PHONY: help start stop restart logs clean shell psql migrate env-check build up pause

COMPOSE ?= docker-compose

help:
	@echo "Quantitative Trading Platform commands:"
	@echo "  make start    Generate .env if needed, build, boot, and migrate the platform"
	@echo "  make stop     Stop all running platform containers"
	@echo "  make restart  Restart the full platform and rerun migrations"
	@echo "  make logs     Follow logs for all services"
	@echo "  make clean    Destructively remove containers, volumes, images, and orphans"
	@echo "  make shell    Open a shell in the API container"
	@echo "  make psql     Open psql in the Postgres container"
	@echo "  make migrate  Run Alembic migrations in the API container"

env-check:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "WARNING: .env was missing, so it was created from .env.example. Fill in API keys later."; \
	fi

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

pause:
	sleep 5

migrate:
	$(COMPOSE) exec -T api alembic upgrade head

start: env-check build up pause migrate
	@echo "Platform started successfully. Dashboard: http://localhost:3000 API: http://localhost:8000"

stop:
	$(COMPOSE) down

restart: stop start

logs:
	$(COMPOSE) logs -f

clean:
	$(COMPOSE) down -v --rmi all --remove-orphans

shell:
	$(COMPOSE) exec api sh

psql:
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
