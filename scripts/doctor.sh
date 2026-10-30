#!/usr/bin/env bash
# Read-only local diagnostics for the ComputeGrid Compose stack.

set -u

API_URL="${COMPUTEGRID_API_URL:-http://127.0.0.1:${COMPUTEGRID_API_PORT:-8000}}"
SERVICES=(postgres rabbitmq migrate api relay worker)
docker_available=0

ok() { printf '  [OK] %s\n' "$*"; }
unhealthy() { printf '  [UNHEALTHY] %s\n' "$*"; }
unreachable() { printf '  [UNREACHABLE] %s\n' "$*"; }
unknown() { printf '  [UNKNOWN] %s\n' "$*"; }

container_id() {
  docker compose ps --all --quiet "$1" 2>/dev/null
}

container_state() {
  local id
  id="$(container_id "$1")"
  if [[ -z "$id" ]]; then
    printf 'missing'
    return
  fi
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$id" 2>/dev/null || printf 'unknown'
}

http_check() {
  local label="$1"
  local path="$2"
  local response http_code
  if ! command -v curl >/dev/null 2>&1; then
    unknown "$label (curl is not installed)"
    return
  fi
  response="$(curl --silent --show-error --max-time 3 --output /dev/null \
    --write-out '%{http_code}' "${API_URL}${path}" 2>/dev/null)" || response="000"
  http_code="${response:-000}"
  case "$http_code" in
    200) ok "$label (${API_URL}${path})" ;;
    000) unreachable "$label (${API_URL}${path})" ;;
    *) unhealthy "$label returned HTTP ${http_code} (${API_URL}${path})" ;;
  esac
}

printf 'ComputeGrid doctor (read-only)\n'
printf '\nCompose services\n'
if ! command -v docker >/dev/null 2>&1; then
  unreachable "Docker CLI is not installed"
elif ! docker compose version >/dev/null 2>&1; then
  unreachable "Docker Compose is unavailable"
elif ! docker info >/dev/null 2>&1; then
  unreachable "Docker daemon is unavailable"
else
  docker_available=1
  for service in "${SERVICES[@]}"; do
    state="$(container_state "$service")"
    if [[ "$service" == "migrate" && "$state" == "exited" ]]; then
      migration_id="$(container_id migrate)"
      migration_exit_code="$(docker inspect --format '{{.State.ExitCode}}' \
        "$migration_id" 2>/dev/null || printf 'unknown')"
      if [[ "$migration_exit_code" == "0" ]]; then
        ok "migrate one-shot completed successfully"
      else
        unhealthy "migrate one-shot exited with code $migration_exit_code"
      fi
      continue
    fi
    case "$state" in
      running | healthy) ok "$service container is $state" ;;
      unhealthy | exited | dead) unhealthy "$service container is $state" ;;
      missing) unreachable "$service container is not running" ;;
      *) unknown "$service container state is $state" ;;
    esac
  done
fi

printf '\nAPI probes\n'
http_check "liveness" "/healthz"
http_check "readiness" "/readyz"

printf '\nDependencies\n'
if [[ "$docker_available" -ne 1 ]]; then
  unknown "PostgreSQL probe skipped without Docker"
  unknown "RabbitMQ probe and queue details skipped without Docker"
else
  postgres_state="$(container_state postgres)"
  if [[ "$postgres_state" != "running" && "$postgres_state" != "healthy" ]]; then
    unreachable "PostgreSQL container is not available for pg_isready"
  elif docker compose exec -T postgres sh -c \
    'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    ok "PostgreSQL accepts connections"
  else
    unhealthy "PostgreSQL rejected pg_isready"
  fi

  rabbit_state="$(container_state rabbitmq)"
  if [[ "$rabbit_state" != "running" && "$rabbit_state" != "healthy" ]]; then
    unreachable "RabbitMQ container is not available for diagnostics"
  elif docker compose exec -T rabbitmq rabbitmq-diagnostics -q ping >/dev/null 2>&1; then
    ok "RabbitMQ responds to diagnostics"
    printf '  Queue depth (ready, unacknowledged, consumers):\n'
    if ! docker compose exec -T rabbitmq rabbitmqctl list_queues \
      name messages_ready messages_unacknowledged consumers --formatter table 2>/dev/null; then
      unknown "RabbitMQ queue counters could not be read"
    fi
  else
    unhealthy "RabbitMQ did not respond to diagnostics"
  fi
fi

printf '\nProcesses\n'
if [[ "$docker_available" -ne 1 ]]; then
  unknown "Worker and relay probes skipped without Docker"
else
  worker_state="$(container_state worker)"
  if [[ "$worker_state" != "running" && "$worker_state" != "healthy" ]]; then
    unreachable "worker process is not running"
  elif docker compose exec -T worker python -m celery \
    -A computegrid_api.celery_app:celery_app inspect ping --timeout=3 2>/dev/null \
    | grep -q 'pong'; then
    ok "Celery worker answered ping"
  else
    unhealthy "worker container is running but did not answer Celery ping"
  fi

  relay_state="$(container_state relay)"
  if [[ "$relay_state" == "running" || "$relay_state" == "healthy" ]]; then
    unknown "relay process is running; no active relay probe exists"
  elif [[ "$relay_state" == "missing" ]]; then
    unreachable "relay process is not running"
  else
    unhealthy "relay container is $relay_state"
  fi
fi

printf '\nRecent application errors\n'
if [[ "$docker_available" -ne 1 ]]; then
  unknown "application logs unavailable without Docker"
else
  if ! recent_logs="$(docker compose logs --no-color --tail=100 api relay worker 2>/dev/null)"; then
    unknown "application logs could not be read"
  else
    errors="$(printf '%s\n' "$recent_logs" \
      | grep -E '"severity":"(ERROR|CRITICAL)"|(^|[[:space:]])(ERROR|CRITICAL)([[:space:]]|$)' \
      | tail -n 10 || true)"
    if [[ -n "$errors" ]]; then
      unhealthy "recent error-level application logs found"
      printf '%s\n' "$errors" | sed 's/^/    /'
    else
      ok "no error-level entries in the last 100 API/relay/worker log lines"
    fi
  fi
fi
