#!/usr/bin/env bash
# Generate stable local credentials and apply the ComputeGrid Kubernetes Secret.

set -euo pipefail

readonly k8s_context="kind-computegrid"
readonly k8s_namespace="computegrid"
readonly secret_file=".env.k8s"

if ! command -v kubectl >/dev/null 2>&1; then
  printf 'error: kubectl is required\n' >&2
  exit 1
fi
if ! command -v base64 >/dev/null 2>&1; then
  printf 'error: base64 is required\n' >&2
  exit 1
fi

if ! kubectl config get-contexts -o name 2>/dev/null | grep -Fxq "$k8s_context"; then
  printf "error: kubectl context '%s' is missing\n" "$k8s_context" >&2
  exit 1
fi

if ! kubectl --context "$k8s_context" get namespace "$k8s_namespace" >/dev/null 2>&1; then
  printf "error: namespace '%s' must be created before its Secret\n" "$k8s_namespace" >&2
  exit 1
fi

if [[ ! -f "$secret_file" ]]; then
  if kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get secret computegrid-secrets >/dev/null 2>&1; then
    printf "error: Secret exists but '%s' is missing; refusing to replace credentials\n" \
      "$secret_file" >&2
    printf 'Recover the local file or intentionally remove the ComputeGrid namespace first.\n' >&2
    exit 1
  fi
  umask 077
  postgres_password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  rabbitmq_password="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  development_token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  {
    printf 'POSTGRES_USER=computegrid\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'RABBITMQ_USER=computegrid\n'
    printf 'RABBITMQ_PASSWORD=%s\n' "$rabbitmq_password"
    printf 'COMPUTEGRID_DEVELOPMENT_API_TOKEN=%s\n' "$development_token"
  } > "$secret_file"
  printf "Created ignored local credential file '%s'.\n" "$secret_file"
else
  chmod 600 "$secret_file"
  printf "Reusing ignored local credential file '%s'.\n" "$secret_file"
fi

postgres_user=""
postgres_password=""
rabbitmq_user=""
rabbitmq_password=""
development_token=""
while IFS='=' read -r key value; do
  case "$key" in
    POSTGRES_USER) postgres_user="$value" ;;
    POSTGRES_PASSWORD) postgres_password="$value" ;;
    RABBITMQ_USER) rabbitmq_user="$value" ;;
    RABBITMQ_PASSWORD) rabbitmq_password="$value" ;;
    COMPUTEGRID_DEVELOPMENT_API_TOKEN) development_token="$value" ;;
  esac
done < "$secret_file"

for value_name in \
  postgres_user postgres_password rabbitmq_user rabbitmq_password development_token; do
  value="${!value_name}"
  if [[ -z "$value" || "$value" == *[!A-Za-z0-9]* ]]; then
    printf 'error: %s must be non-empty and contain only ASCII letters and digits\n' \
      "$value_name" >&2
    exit 1
  fi
done

if kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
  get secret computegrid-secrets >/dev/null 2>&1 \
  && kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get statefulset postgres rabbitmq >/dev/null 2>&1; then
  existing_postgres_user="$(kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get secret computegrid-secrets -o jsonpath='{.data.POSTGRES_USER}' | base64 --decode)"
  existing_postgres_password="$(kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get secret computegrid-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 --decode)"
  existing_rabbitmq_user="$(kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get secret computegrid-secrets -o jsonpath='{.data.RABBITMQ_USER}' | base64 --decode)"
  existing_rabbitmq_password="$(kubectl --context "$k8s_context" --namespace "$k8s_namespace" \
    get secret computegrid-secrets -o jsonpath='{.data.RABBITMQ_PASSWORD}' | base64 --decode)"
  if [[ "$postgres_user" != "$existing_postgres_user" \
    || "$postgres_password" != "$existing_postgres_password" \
    || "$rabbitmq_user" != "$existing_rabbitmq_user" \
    || "$rabbitmq_password" != "$existing_rabbitmq_password" ]]; then
    printf 'error: local database or broker credentials differ from initialized resources\n' >&2
    printf 'Refusing to rotate credentials or disrupt persistent data.\n' >&2
    exit 1
  fi
fi

secret_tmp="$(mktemp -d)"
cleanup() {
  find "$secret_tmp" -type f -delete
  rmdir "$secret_tmp"
}
trap cleanup EXIT
umask 077
printf %s "$postgres_user" > "$secret_tmp/POSTGRES_USER"
printf %s "$postgres_password" > "$secret_tmp/POSTGRES_PASSWORD"
printf %s "$rabbitmq_user" > "$secret_tmp/RABBITMQ_USER"
printf %s "$rabbitmq_password" > "$secret_tmp/RABBITMQ_PASSWORD"
printf %s "$development_token" > "$secret_tmp/COMPUTEGRID_DEVELOPMENT_API_TOKEN"

kubectl --context "$k8s_context" --namespace "$k8s_namespace" create secret generic \
  computegrid-secrets \
  --from-file="POSTGRES_USER=$secret_tmp/POSTGRES_USER" \
  --from-file="POSTGRES_PASSWORD=$secret_tmp/POSTGRES_PASSWORD" \
  --from-file="RABBITMQ_USER=$secret_tmp/RABBITMQ_USER" \
  --from-file="RABBITMQ_PASSWORD=$secret_tmp/RABBITMQ_PASSWORD" \
  --from-file="COMPUTEGRID_DEVELOPMENT_API_TOKEN=$secret_tmp/COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  --dry-run=client -o yaml \
  | kubectl --context "$k8s_context" --namespace "$k8s_namespace" apply -f -

printf 'Applied Secret without printing credential values.\n'
