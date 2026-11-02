UV ?= uv
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
NPM ?= npm
WEB_DIR ?= apps/web
override K8S_CONTEXT := kind-computegrid
override K8S_NAMESPACE := computegrid
override K8S_IMAGE := computegrid:local

.PHONY: install install-web install-all run web-dev db-up db-down db-logs broker-up dev-deps-up dev-up dev-down dev-logs doctor k8s-create k8s-status k8s-delete k8s-image k8s-configure k8s-dependencies k8s-migrate k8s-apps k8s-app-status k8s-deploy k8s-port-forward k8s-remove migrate relay worker test test-integration test-fault test-e2e test-all lint format typecheck backend-check web-test web-typecheck web-build web-check check

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

doctor:
	bash scripts/doctor.sh

k8s-create:
	@set -eu; \
	command -v docker >/dev/null 2>&1 || { echo "error: Docker is required" >&2; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "error: Docker is not reachable; check Docker Desktop WSL integration" >&2; exit 1; }; \
	command -v kind >/dev/null 2>&1 || { echo "error: kind is required; see README.md" >&2; exit 1; }; \
	command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required; see README.md" >&2; exit 1; }; \
	if kind get clusters 2>/dev/null | grep -Fxq computegrid; then \
		echo "kind cluster 'computegrid' already exists; no changes made"; \
		echo "Run 'make k8s-status' to inspect it"; \
		exit 0; \
	fi; \
	if kubectl config get-contexts -o name 2>/dev/null | grep -Fxq kind-computegrid; then \
		echo "error: kubectl context 'kind-computegrid' exists without a matching kind cluster" >&2; \
		echo "Inspect the stale context manually; no changes were made" >&2; \
		exit 1; \
	fi; \
	kind create cluster --name computegrid --wait 120s; \
	kubectl --context kind-computegrid cluster-info

k8s-status:
	@set -eu; \
	command -v docker >/dev/null 2>&1 || { echo "error: Docker is required" >&2; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "error: Docker is not reachable; check Docker Desktop WSL integration" >&2; exit 1; }; \
	command -v kind >/dev/null 2>&1 || { echo "error: kind is required; see README.md" >&2; exit 1; }; \
	command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required; see README.md" >&2; exit 1; }; \
	kind get clusters 2>/dev/null | grep -Fxq computegrid || { echo "error: kind cluster 'computegrid' does not exist" >&2; exit 1; }; \
	kubectl config get-contexts -o name 2>/dev/null | grep -Fxq kind-computegrid || { echo "error: kubectl context 'kind-computegrid' is missing" >&2; exit 1; }; \
	echo "Cluster: computegrid"; \
	kubectl --context kind-computegrid cluster-info; \
	echo; \
	echo "Nodes:"; \
	kubectl --context kind-computegrid get nodes -o wide; \
	echo; \
	echo "System Pods:"; \
	kubectl --context kind-computegrid get pods -A -o wide

k8s-delete:
	@set -eu; \
	command -v docker >/dev/null 2>&1 || { echo "error: Docker is required" >&2; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "error: Docker is not reachable; check Docker Desktop WSL integration" >&2; exit 1; }; \
	command -v kind >/dev/null 2>&1 || { echo "error: kind is required; see README.md" >&2; exit 1; }; \
	if ! kind get clusters 2>/dev/null | grep -Fxq computegrid; then \
		echo "kind cluster 'computegrid' does not exist; no changes made"; \
		exit 0; \
	fi; \
	kind delete cluster --name computegrid

k8s-image:
	@command -v docker >/dev/null 2>&1 || { echo "error: Docker is required" >&2; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "error: Docker is not reachable" >&2; exit 1; }
	@command -v kind >/dev/null 2>&1 || { echo "error: kind is required" >&2; exit 1; }
	@kind get clusters 2>/dev/null | grep -Fxq computegrid || { echo "error: kind cluster 'computegrid' does not exist" >&2; exit 1; }
	docker build --tag $(K8S_IMAGE) .
	kind load docker-image $(K8S_IMAGE) --name computegrid

k8s-configure:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl config get-contexts -o name 2>/dev/null | grep -Fxq $(K8S_CONTEXT) || { echo "error: context '$(K8S_CONTEXT)' is missing" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) apply -f k8s/namespace.yaml
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) apply -f k8s/configmap.yaml
	bash scripts/k8s-create-secret.sh

k8s-dependencies:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl --context $(K8S_CONTEXT) get namespace $(K8S_NAMESPACE) >/dev/null 2>&1 || { echo "error: run 'make k8s-configure' first" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) apply -f k8s/postgres.yaml
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) apply -f k8s/rabbitmq.yaml
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status statefulset/postgres --timeout=180s
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status statefulset/rabbitmq --timeout=240s

k8s-migrate:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) get statefulset postgres >/dev/null 2>&1 || { echo "error: run 'make k8s-dependencies' first" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status statefulset/postgres --timeout=180s
	@echo "Recreating only the one-shot ComputeGrid migration Job; persistent data is not reset."
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) delete job computegrid-migrate --ignore-not-found --wait=true
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) apply -f k8s/migrate.yaml
	@kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) wait --for=condition=complete job/computegrid-migrate --timeout=180s || { \
		echo "error: migration did not complete; recent logs follow" >&2; \
		kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) logs job/computegrid-migrate --all-containers=true --tail=100 >&2 || true; \
		exit 1; \
	}

k8s-apps:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) wait --for=condition=complete job/computegrid-migrate --timeout=1s >/dev/null 2>&1 || { echo "error: a completed migration Job is required; run 'make k8s-migrate' first" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status statefulset/postgres --timeout=180s
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status statefulset/rabbitmq --timeout=240s
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) apply -f k8s/applications.yaml
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout restart deployment/computegrid-api deployment/computegrid-relay deployment/computegrid-worker
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status deployment/computegrid-api --timeout=180s
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status deployment/computegrid-relay --timeout=180s
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) rollout status deployment/computegrid-worker --timeout=240s

k8s-app-status:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl config get-contexts -o name 2>/dev/null | grep -Fxq $(K8S_CONTEXT) || { echo "error: context '$(K8S_CONTEXT)' is missing" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) get statefulsets,deployments,jobs,pods,services,pvc -o wide

k8s-deploy:
	$(MAKE) k8s-image
	$(MAKE) k8s-configure
	$(MAKE) k8s-dependencies
	$(MAKE) k8s-migrate
	$(MAKE) k8s-apps
	$(MAKE) k8s-app-status

k8s-port-forward:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl config get-contexts -o name 2>/dev/null | grep -Fxq $(K8S_CONTEXT) || { echo "error: context '$(K8S_CONTEXT)' is missing" >&2; exit 1; }
	@kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) get service computegrid-api >/dev/null 2>&1 || { echo "error: ComputeGrid API Service is not deployed" >&2; exit 1; }
	kubectl --context $(K8S_CONTEXT) --namespace $(K8S_NAMESPACE) port-forward service/computegrid-api 8001:8000

k8s-remove:
	@command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl is required" >&2; exit 1; }
	@kubectl config get-contexts -o name 2>/dev/null | grep -Fxq $(K8S_CONTEXT) || { echo "error: context '$(K8S_CONTEXT)' is missing" >&2; exit 1; }
	@if [ "$(CONFIRM)" != "computegrid" ]; then \
		echo "Refusing to delete the namespace and its PVC data." >&2; \
		echo "Run 'make k8s-remove CONFIRM=computegrid' to remove only the ComputeGrid namespace." >&2; \
		exit 1; \
	fi
	kubectl --context $(K8S_CONTEXT) delete namespace $(K8S_NAMESPACE) --ignore-not-found=true

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

