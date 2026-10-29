UV ?= uv
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
NPM ?= npm
WEB_DIR ?= apps/web

.PHONY: install install-web install-all run web-dev db-up db-down db-logs broker-up dev-deps-up dev-up dev-down dev-logs migrate relay worker test test-integration test-fault test-e2e test-all lint format typecheck backend-check web-test web-typecheck web-build web-check check

install:
	$(UV) sync --python 3.12 --extra dev

install-web:
	$(NPM) --prefix $(WEB_DIR) install

install-all: install install-web

run:
	$(PYTHON) -m uvicorn computegrid_api.main:app --app-dir apps/api --reload

web-dev:
	$(NPM) --prefix $(WEB_DIR) run dev

db-up:
	docker compose up -d --wait postgres

db-down:
	docker compose down

db-logs:
	docker compose logs -f postgres

broker-up:
	docker compose up -d --wait rabbitmq

dev-deps-up:
	docker compose up -d --wait postgres rabbitmq

dev-up:
	docker compose up -d --build --wait

dev-down:
	docker compose down

dev-logs:
	docker compose logs -f api relay worker rabbitmq

migrate:
	$(PYTHON) -m alembic upgrade head

relay:
	$(PYTHON) -m computegrid_api.outbox_relay

worker:
	$(PYTHON) -m celery -A computegrid_api.celery_app:celery_app worker --loglevel=INFO

test:
	$(PYTHON) -m pytest tests/unit

test-integration:
	$(PYTHON) -m pytest tests/integration

test-fault:
	$(PYTHON) -m pytest tests/fault

test-e2e:
	$(PYTHON) -m pytest tests/e2e

test-all: test test-integration test-fault test-e2e

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m mypy

backend-check: lint typecheck test

web-test:
	$(NPM) --prefix $(WEB_DIR) run test

web-typecheck:
	$(NPM) --prefix $(WEB_DIR) run typecheck

web-build:
	$(NPM) --prefix $(WEB_DIR) run build

web-check:
	$(NPM) --prefix $(WEB_DIR) run check

check: backend-check web-check

