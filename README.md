
# ComputeGrid

**A fault-tolerant distributed job-processing platform built with Python, FastAPI, PostgreSQL, RabbitMQ, Celery, and React.**

> **Status (October 2026):** M1–M4D and M5A-1/M5A-2 are complete and verified. M5B's animated live operations console is implemented and awaiting local verification. ComputeGrid has been deployed with Docker Compose and on a local single-node Kubernetes cluster using kind. Verified capabilities include distributed execution, durable retries, worker crash recovery, horizontal worker scaling, Kubernetes self-healing, rolling restarts, in-flight worker failure recovery, authenticated observability, and isolated anonymous demo access. Backend and frontend CI pass on GitHub Actions.
>
> **Next:** Verify M5C Computational Load Demo (implemented, verification pending), then consider M5D failure controls and public-demo deployment. AWS EKS remains optional.
>
> **Project context:** ComputeGrid is an independent portfolio project inspired by the reliability and infrastructure challenges of cloud compute services. It is not a D-Wave product, an implementation of D-Wave's Hybrid Solver Service, or affiliated with D-Wave. All current workloads execute on classical CPUs; no QPU or external quantum solver is used.

## 1. Purpose

The live operations console uses two independent HTTP polling loops. Visitor job
states and persisted attempt-to-worker assignments refresh approximately every
500ms through `/v1/operations/summary?flow_only=true`. This bounded, session-scoped
database read performs no Celery inspection, RabbitMQ calls, or global count scan.
Anonymous flow reads have a separate 150-request/minute allowance and retain the
existing global anonymous request budget and opaque worker identifiers.

Worker discovery, RabbitMQ queue counters, and aggregate counts use the normal
summary endpoint, with three seconds between completed requests. Those metrics
can therefore lag the job blocks; worker-wide idle/busy status remains the last
inspection result unless a visitor's persisted running assignment confirms busy.
Demo progress refreshes once per second while feeding or draining, and every
three seconds when idle. Recent Jobs refreshes on progress changes; selected-job
details retain their one-second refresh. No execution or admission settings change.

Flow requests are serialized, abort on cleanup, and reject late replies after
session changes. Slow monitoring responses cannot replace newer flow state.
Existing CSS transitions animate committed state immediately; polling may skip
short-lived states, and the UI does not invent intermediate transitions.

ComputeGrid accepts computational jobs through a REST API, persists their state, dispatches them to background workers, and recovers safely from failures.

The engineering focus is on:

- Distributed job execution and queue-based load distribution.
- Durable state transitions and transactional messaging.
- Retry policies, worker crash recovery, and stale-result protection.
- Automated testing and continuous integration.
- Logging, health checks, and operational troubleshooting.
- Container orchestration and horizontal scaling.

The project prioritizes distributed-systems correctness and practical operations over introducing unnecessary infrastructure.

### Engineering questions

- How can an API accept a job without risking message loss between the database and broker?
- What happens when messages are delivered more than once?
- How should the system respond when a worker crashes during execution?
- How can a recovered job reject results from an outdated worker?
- How can multiple workers share a queue and process jobs concurrently?
- How can the system be tested, monitored, diagnosed, and deployed with minimal operational complexity?

## 2. Architecture

```text
                  React Dashboard / API Client
                              |
                              v
                        FastAPI REST API
                     Validation / Dev Auth
                        Idempotency Keys
                              |
                              v
                       PostgreSQL
                +---------------------------+
                | jobs                      |
                | job_attempts              |
                | outbox_events             |
                +---------------------------+
                              |
                              v
                         Outbox Relay
                   Publish + Confirmation
                    Expired Lease Recovery
                              |
                              v
                           RabbitMQ
                         Shared Queue
                              |
                  +-----------+-----------+
                  |           |           |
                  v           v           v
               Worker 1    Worker 2    Worker N
                  |           |           |
                  +-----------+-----------+
                              |
                              v
                   PostgreSQL State/Result
```

### Core components

**FastAPI**

- Validates incoming job requests.
- Preserves developer bearer-token authentication and optionally provides
  isolated, signed anonymous demo sessions.
- Supports client-scoped idempotency keys.
- Persists jobs and outbox events atomically.
- Exposes job status, results, and execution history.

**PostgreSQL**

- Stores durable job state and results.
- Records individual execution attempts.
- Stores transactional outbox events.
- Enforces state transitions and fencing through conditional updates.

**Outbox Relay**

- Reads pending events from PostgreSQL.
- Publishes work notifications to RabbitMQ with publisher confirmation.
- Recovers expired worker leases.
- Coordinates retry scheduling without treating the broker as the source of truth.

**RabbitMQ**

- Provides a shared task queue.
- Distributes work among competing Celery consumers.
- Supports independent worker instances.

**Celery Workers**

- Claim and execute jobs.
- Persist results and failure information.
- Respect retry and fencing rules.
- Support concurrent execution across processes and worker instances.

**React Dashboard**

- Submits supported computational jobs.
- Displays job status and results.
- Shows persisted execution attempts and recovery history.
- Provides model-specific input and output interfaces.

### Source of truth

PostgreSQL is authoritative for job execution state.

RabbitMQ transports work notifications, but the presence or acknowledgment of a message does not independently prove that a job completed successfully.

## 3. Reliability Model

### Job lifecycle

```text
ACCEPTED
    |
    v
 QUEUED
    |
    v
 RUNNING ----------------------+
    |                         |
    v                         v
SUCCEEDED                 RETRY_WAIT
                              |
                              v
                            QUEUED
                         (next attempt)

When retry limits are exhausted:
    -> FAILED

An expired execution attempt:
    RUNNING -> ABANDONED

The parent job then retries or fails according
to the configured recovery and retry policy.
```

### Transactional Outbox

Job creation and outbox-event creation occur in the same PostgreSQL transaction.

This avoids the dual-write problem where an API could persist a job but fail before publishing its work notification.

The relay publishes committed events to RabbitMQ and records publication progress.

The system tolerates repeated publication attempts and duplicate message delivery.

### Retry Handling

- Distinguishes retryable and permanent failures.
- Uses bounded exponential backoff with jitter.
- Enforces a maximum attempt count.
- Persists retry scheduling and terminal failure states.
- Rejects stale or duplicate execution attempts from overwriting current state.

### Worker Leases and Crash Recovery

Each claimed job attempt receives a fixed-duration execution lease.

If a worker crashes or stops responding:

1. The attempt's lease can expire.
2. The outbox relay identifies the expired attempt.
3. The attempt is recorded as `ABANDONED`.
4. The job is scheduled for retry or marked failed if attempts are exhausted.
5. A subsequent worker can claim and execute the job.

The implementation uses fencing checks to prevent an outdated attempt from committing a result after recovery. Detailed evidence from the verified Kubernetes in-flight failure experiment is recorded once in the M4D section.

### Guarantee Boundary

ComputeGrid provides **at-least-once task delivery with guarded durable state transitions**, not exactly-once physical execution.

Important limitations:

- A job may physically execute more than once.
- Duplicate or late execution must not overwrite the authoritative result.
- External side effects require independent idempotency protection.
- Leases are fixed-duration and are not currently renewed through heartbeats.
- Supported workloads must have appropriately bounded execution times.

These boundaries are intentional and documented rather than hidden behind claims of exactly-once execution.

## 4. Supported Computational Jobs

ComputeGrid provides four allowlisted job types.

All workloads execute locally on classical CPU workers.

The optimization workloads demonstrate how mathematical models can be represented and processed through a fault-tolerant distributed service. They do not perform quantum computation.

### 4.1 Monte Carlo π

**Job type:** `monte_carlo_pi`

Estimates π through randomized numerical sampling.

Applications of Monte Carlo methods include:

- Numerical integration.
- Statistical simulation.
- Risk and uncertainty analysis.

The worker executes a bounded sampling workload and returns the estimated value and associated execution result.

### 4.2 Weighted Max-Cut

**Job type:** `weighted_max_cut`

Finds a partition of an undirected weighted graph that maximizes the total weight of edges crossing between two groups.

The worker:

1. Validates a bounded weighted graph.
2. Converts the Max-Cut objective into a QUBO/BQM representation.
3. Solves the model using exact classical enumeration.
4. Returns the optimal partition, cut weight, energy, and model information.

For binary variables \(x_i \in \{0,1\}\), the objective is:

\[
\max \sum_{(i,j)\in E} w_{ij}(x_i+x_j-2x_ix_j)
\]

The equivalent QUBO minimizes the negative of this objective.

**Current scope:** 2–16 vertices, with validated finite positive edge weights. The exact solver uses Max-Cut symmetry to reduce redundant assignments.

### 4.3 Binary Quadratic Model (BQM)

**Job type:** `bqm`

A Binary Quadratic Model represents an optimization problem using linear and pairwise interactions between decision variables.

Supported variable types:

- `BINARY`: \(x_i \in \{0,1\}\)
- `SPIN`: \(s_i \in \{-1,+1\}\)

The model includes linear biases, quadratic biases, and an optional offset.

A classical CPU worker evaluates possible assignments and returns the assignment with minimum energy.

Potential applications include:

- Binary assignment and selection problems.
- Combinatorial optimization.
- Ising spin-system models.

**Current scope:** At most 16 variables. The solver uses exact enumeration with exponential worst-case scaling.

### 4.4 Constrained Quadratic Model (CQM)

**Job type:** `cqm`

Represents an optimization problem with an objective and explicit feasibility constraints.

The current implementation supports a deliberately restricted subset:

- Binary decision variables.
- Linear objectives.
- Linear constraints using `<=`, `>=`, or `==`.
- Minimization and maximization.

A classical CPU worker enumerates assignments, checks feasibility, and selects the optimal feasible solution.

Potential applications include:

- Resource allocation.
- Budget-constrained selection.
- Small scheduling and assignment problems.

**Current scope:** At most 16 binary variables and 32 constraints. General quadratic objectives, quadratic constraints, and arbitrary integer/real variables are not supported.

A mathematically infeasible CQM is a valid completed computation. It is represented by a successful job execution with an optimization result indicating `INFEASIBLE`, rather than by a worker execution failure.

### Optimization Scope

Exact classical enumeration has exponential complexity in the number of decision variables.

For larger problems, alternative approaches can include:

- Classical approximation algorithms and heuristics.
- Mixed-integer optimization solvers.
- Quantum annealing.
- Quantum-classical hybrid optimization.

ComputeGrid does not claim that quantum approaches are universally faster or better than classical methods.

## 5. Implemented Milestones

### M1 — API, Persistence, and Dashboard

**Status: Complete**

- FastAPI REST endpoints for job submission and retrieval.
- PostgreSQL persistence using SQLAlchemy and Alembic.
- Development bearer-token authentication.
- Client-scoped idempotency keys.
- Atomic job and outbox-event creation.
- Initial React/TypeScript dashboard.
- Docker Compose development environment.
- Unit and integration tests.

### M2A — Distributed Job Execution

**Status: Complete**

- RabbitMQ message broker.
- Celery workers.
- Transactional outbox relay.
- Publisher confirmations.
- Persisted job attempts and results.
- Duplicate-delivery handling.
- Integration and end-to-end tests.

### M2B — Retry and Failure Handling

**Status: Complete**

- Retryable and permanent error classification.
- Bounded exponential retry with jitter.
- Durable retry scheduling.
- Maximum attempt enforcement.
- Terminal failure handling.
- Stale-message protection.

### M2C — Crash Recovery and Fencing

**Status: Complete**

- Fixed-duration worker leases.
- Expired-attempt recovery through the relay.
- `ABANDONED` attempt history.
- Retry after worker failure.
- Fencing against stale results.
- Automated fault-injection tests.
- Successful real in-flight worker `SIGKILL` recovery demonstration.

### M2.5 — Dashboard and Weighted Max-Cut

**Status: Complete**

- Improved dashboard layout and job selection.
- Weighted Max-Cut job support.
- QUBO formulation and exact CPU solver.
- Model-specific result presentation.
- Persisted execution history.
- Existing Monte Carlo support preserved.

### M2.6 — BQM and CQM Optimization Models

**Status: Complete**

- Generic BINARY and SPIN BQM support.
- Restricted binary/linear CQM support.
- Shared classical optimization infrastructure.
- Feasibility and optimal-solution reporting.
- Dedicated model-selection UI and examples.
- Validation and automated tests.
- No external solver or quantum hardware dependency.

### M3 — CI and Lightweight Operations

**Status: Complete**

- GitHub Actions continuous integration.
- Automated backend and frontend quality checks.
- Dependency-free structured JSON logging.
- Job-correlated lifecycle events.
- API liveness and readiness checks.
- Docker Compose health checks.
- Read-only operational diagnostics through `make doctor`.
- Verified PostgreSQL and Worker failure detection.

### M4A — Local Kubernetes Environment

**Status: Complete — created and verified with kind and Kubernetes v1.37.0**

M4A provides a minimal single-node Kubernetes learning environment without
deploying ComputeGrid services:

- **kind** runs Kubernetes Nodes as Docker containers. The cluster is always
  named `computegrid`.
- **kubectl** is the command-line client used to inspect Kubernetes resources.
- A **Node** is a machine participating in a cluster; this local cluster has one
  control-plane Node backed by a Docker container.
- A **Pod** is Kubernetes' smallest deployable unit. M4A initially verified only
  Kubernetes system Pods; M4B later added the ComputeGrid workloads.
- A **context** selects a cluster, credentials, and default namespace from the
  kubeconfig file. Project commands always pass the explicit
  `kind-computegrid` context and never rely on the current context.

No kind configuration file is needed for the default single-node topology.

#### Install kind and kubectl in WSL2

Use the official [kind Quick Start](https://kind.sigs.k8s.io/docs/user/quick-start/)
and [kubectl Linux installation guide](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/).
For an x86-64 WSL2 distribution, the following installs both binaries in the
user-local executable directory without modifying the system Python:

```bash
mkdir -p "$HOME/.local/bin"

curl -Lo "$HOME/.local/bin/kind" \
  https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64
chmod +x "$HOME/.local/bin/kind"

KUBECTL_VERSION="$(curl -L -s https://dl.k8s.io/release/stable.txt)"
curl -Lo /tmp/kubectl \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
curl -Lo /tmp/kubectl.sha256 \
  "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum --check
install -m 0755 /tmp/kubectl "$HOME/.local/bin/kubectl"
rm /tmp/kubectl /tmp/kubectl.sha256

export PATH="$HOME/.local/bin:$PATH"
kind version
kubectl version --client
```

Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` if that directory is
not already on the WSL shell's `PATH`. ARM64 users should use the official
`arm64` downloads instead.

Docker Desktop must be running with WSL integration enabled for the active
distribution. Confirm that `docker info` works inside WSL before creating the
cluster.

#### Create and inspect the cluster

```bash
make k8s-create
make k8s-status
```

`k8s-create` checks Docker, kind, and kubectl before making changes. It will not
delete or recreate an existing `computegrid` cluster, and it refuses to proceed
if a stale `kind-computegrid` context exists without the corresponding kind
cluster. `k8s-status` displays cluster information, the Node, and all system
Pods. The equivalent focused inspection commands are:

```bash
kubectl --context kind-computegrid get nodes
kubectl --context kind-computegrid get pods -A
```

A healthy cluster normally reports one `computegrid-control-plane` Node as
`Ready`; system Pods in namespaces such as `kube-system` and `local-path-storage`
should settle in `Running` status.

Remove only this project's local cluster with:

```bash
make k8s-delete
```

The delete target passes the exact `computegrid` name to kind and treats an
already-absent cluster as a safe no-op. It never deletes another kind cluster.
M4B builds on this environment using plain Kubernetes manifests for the application and its dependencies. Creating the cluster through M4A alone does not deploy ComputeGrid services; application resources are provisioned separately through the M4B deployment workflow.

### M4B — Local Backend Deployment

**Status: Complete — deployed and verified on the local kind cluster**

M4B deploys the complete backend to the existing `computegrid` kind cluster
while the React/Vite frontend remains on the WSL2 host:

```text
WSL2 browser / curl
        |
 kubectl port-forward localhost:8001
        |
 computegrid-api Service -> API Deployment
        |                         |
        +----> postgres Service -> PostgreSQL StatefulSet + PVC
                                  |
                         transactional outbox
                                  |
                         Relay Deployment
                                  |
                         rabbitmq Service
                                  |
                         RabbitMQ StatefulSet + PVC
                                  |
                         Worker Deployment
                                  |
                         PostgreSQL result
```

The plain manifests under `k8s/` use these resource types:

- The `computegrid` **Namespace** isolates all project resources.
- A **ConfigMap** holds non-sensitive queue, retry, lease, client, concurrency,
  and CORS settings.
- The generated `computegrid-secrets` **Secret** holds local PostgreSQL,
  RabbitMQ, and development API credentials.
- Single-replica PostgreSQL and RabbitMQ **StatefulSets** have stable Pod names
  and 1 GiB **PVCs** backed by kind's local-path provisioner.
- Internal **Services** provide stable `postgres`, `rabbitmq`, and
  `computegrid-api` DNS names; separate headless Services provide StatefulSet
  identity. PostgreSQL and RabbitMQ have no host-facing port.
- The one-shot `computegrid-migrate` **Job** runs `alembic upgrade head` before
  application processes start.
- One-replica API, relay, and worker **Deployments** use the same
  `computegrid:local` image with `imagePullPolicy: IfNotPresent`.

The API uses `/healthz` for liveness and PostgreSQL-backed `/readyz` for startup
and readiness. PostgreSQL and RabbitMQ retain their native command probes. The
worker readiness probe uses Celery ping; a temporary broker outage can make the
worker unready without treating the API as dead. The relay has no artificial
health server: Kubernetes restarts it if its PID 1 process exits.

#### Local image, credentials, and deployment order

The deployment uses no registry. Build and load the image directly into kind,
then apply each stage in order:

```bash
make k8s-image
make k8s-configure
make k8s-dependencies
make k8s-migrate
make k8s-apps
make k8s-app-status
```

The aggregate command performs those same steps sequentially:

```bash
make k8s-deploy
```

`k8s-configure` creates `.env.k8s` on first use with randomly generated,
URL-safe local credentials, mode `0600`. The file is ignored by Git and reused
on later deployments so credentials remain compatible with existing database
and broker volumes. It is converted to a Kubernetes Secret through temporary
files; credential values are not printed. Do not change the stored database or
broker passwords after their PVCs have been initialized unless you deliberately
remove the namespace and its data.

Each kubectl command uses context `kind-computegrid` and namespace
`computegrid` where applicable. Re-running the migration recreates only the
migration Job and does not reset PostgreSQL. Re-running the application stage
restarts only the three ComputeGrid Deployments so Pods use the newly loaded
local image.

Expected status includes `postgres-0` and `rabbitmq-0`, one API Pod, one relay
Pod, and one worker Pod in `Running`; application containers with readiness
probes should report ready. The migration Job should report `Complete`, and the
two PVCs should report `Bound`.

#### API access and end-to-end verification

In a dedicated terminal, forward host port 8001 to the internal API Service:

```bash
make k8s-port-forward
```

Then, from another repository-root shell:

```bash
curl --fail http://127.0.0.1:8001/healthz
curl --fail http://127.0.0.1:8001/readyz

set -a
. ./.env.k8s
set +a

K8S_REQUEST_ID="kubernetes-m4b-$(date +%s)"
SUBMISSION="$(curl --fail-with-body --silent --show-error \
  -X POST http://127.0.0.1:8001/v1/jobs \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $K8S_REQUEST_ID" \
  -d '{"type":"monte_carlo_pi","parameters":{"samples":10000,"seed":42}}')"
printf '%s\n' "$SUBMISSION" | python3 -m json.tool
JOB_ID="$(printf '%s' "$SUBMISSION" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

while :; do
  JOB_JSON="$(curl --fail-with-body --silent --show-error \
    -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
    "http://127.0.0.1:8001/v1/jobs/$JOB_ID")"
  JOB_STATE="$(printf '%s' "$JOB_JSON" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')"
  printf 'job %s: %s\n' "$JOB_ID" "$JOB_STATE"
  case "$JOB_STATE" in
    SUCCEEDED | FAILED | CANCELLED) break ;;
  esac
  sleep 1
done
printf '%s\n' "$JOB_JSON" | python3 -m json.tool
```

#### Verified End-to-End Execution

The complete backend was successfully deployed to the local `kind-computegrid` cluster.

Verification confirmed:

- PostgreSQL and RabbitMQ StatefulSets reached a healthy running state with bound PVCs.
- The database migration Job completed successfully.
- API, relay, and worker Deployments became ready.
- `/healthz` and `/readyz` returned HTTP 200 through the forwarded API Service.
- A Monte Carlo job submitted through the Kubernetes API completed successfully.
- The API, relay, and worker produced job-correlated structured lifecycle events.

The verified execution used 10,000 Monte Carlo samples with seed 42 and returned a π estimate of `3.126`.

The persisted job record contained a `SUCCEEDED` execution attempt associated with a Kubernetes Celery worker.

Structured logs confirmed the following events across services:

- API: `job.submitted`
- Outbox relay: `job.queued`
- Worker: `job.claimed`
- Worker: `job.succeeded`

This validates the complete Kubernetes execution path: API persistence, transactional outbox publication, RabbitMQ delivery, Celery execution, and durable result storage.

The frontend remains on the WSL2 host and connects to the Kubernetes API through port-forwarding. Public hosting is not part of M4.

Inspect readiness and logs with:

```bash
kubectl --context kind-computegrid --namespace computegrid get pods -o wide
kubectl --context kind-computegrid --namespace computegrid logs deployment/computegrid-api --tail=100
kubectl --context kind-computegrid --namespace computegrid logs deployment/computegrid-relay --tail=100
kubectl --context kind-computegrid --namespace computegrid logs deployment/computegrid-worker --tail=100
kubectl --context kind-computegrid --namespace computegrid logs job/computegrid-migrate --tail=100
```

To point the existing frontend at the forwarded API, start Vite after setting:

```bash
VITE_API_BASE_URL=http://localhost:8001 npm --prefix apps/web run dev
```

#### Networking, storage, and production boundary

Docker Compose resolves service names on its private Compose network.
Kubernetes resolves Service DNS names inside the Namespace; the application
therefore connects to `postgres:5432` and `rabbitmq:5672`. ClusterIP Services are
not directly reachable from WSL2, so API access uses an explicit port-forward.

The local-path PVCs survive ordinary Pod replacement and Deployment restarts,
but they are tied to the single kind Node. Deleting the Namespace, its PVCs, or
the kind cluster destroys this local data. There is no replication, backup,
failover, TLS, external secret manager, or high availability. A production
deployment should use managed or properly operated PostgreSQL and RabbitMQ,
durable storage, backups, credential rotation, network policy, and appropriate
availability design rather than these single-instance learning resources.

To remove only ComputeGrid Kubernetes resources, including both PVCs and their
data, while preserving the kind cluster:

```bash
make k8s-remove CONFIRM=computegrid
```

The explicit confirmation prevents accidental persistent-data removal. The
ignored `.env.k8s` credential file remains local unless removed manually.

#### Troubleshooting

All commands remain pinned to the project context and Namespace:

```bash
# Pending Pods or PVCs
kubectl --context kind-computegrid --namespace computegrid get pods,pvc
kubectl --context kind-computegrid --namespace computegrid describe pod POD_NAME
kubectl --context kind-computegrid --namespace computegrid get events --sort-by=.lastTimestamp

# CrashLoopBackOff
kubectl --context kind-computegrid --namespace computegrid logs POD_NAME --all-containers --previous
kubectl --context kind-computegrid --namespace computegrid describe pod POD_NAME

# ImagePullBackOff or stale local image
make k8s-image
make k8s-apps

# Migration failure
kubectl --context kind-computegrid --namespace computegrid describe job computegrid-migrate
kubectl --context kind-computegrid --namespace computegrid logs job/computegrid-migrate --all-containers
kubectl --context kind-computegrid --namespace computegrid get secret computegrid-secrets
```

For `Pending`, inspect scheduling and PVC events. For `CrashLoopBackOff`, inspect
the previous container logs. For `ImagePullBackOff`, confirm the local image was
loaded before restarting the application Deployments. Migration errors usually
indicate PostgreSQL readiness, credentials, DNS, image, or Alembic problems; the
deployment workflow stops before starting API, relay, or worker when migration
does not complete.

## 6. Technology Stack

| Concern | Technology | Purpose |
| --- | --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic | REST API and validation |
| Database | PostgreSQL, SQLAlchemy, Alembic | Durable state, transactions, migrations |
| Messaging | RabbitMQ | Shared task queue |
| Workers | Celery | Concurrent background execution |
| Frontend | React, TypeScript, Vite | Interactive dashboard |
| Backend testing | pytest | Unit, integration, fault, E2E tests |
| Frontend testing | Vitest | Component and API tests |
| Static analysis | Ruff, mypy, TypeScript | Linting and type checks |
| CI | GitHub Actions | Automated validation |
| Logging | Python standard logging, JSON | Cross-service troubleshooting |
| Local infrastructure | Docker Compose | Reproducible development environment |
| Local Kubernetes | kind, kubectl | Verified single-node backend orchestration |
| Developer tooling | Make, Bash | Testing and diagnostics |

## 7. Running Locally

### Prerequisites

- Docker Engine with Docker Compose.
- Python 3.12 and the project's Python dependency toolchain.
- Node.js and npm.
- Git.
- kind and kubectl for the local Kubernetes deployment workflow.

No AWS credentials, D-Wave Leap access, or quantum hardware are required.

### Environment Configuration

From the repository root:

```bash
cp .env.example .env
```

Edit `.env` to configure local database and broker credentials, connection
settings, development API token, optional anonymous demo controls, and CORS
origins.

Never commit `.env` or use development credentials for a public deployment.

### Start the Backend Stack

```bash
make dev-up
```

This starts the Docker Compose services, including database migration, API, outbox relay, RabbitMQ, and Celery worker.

Inspect running services:

```bash
docker compose ps
```

View service logs:

```bash
docker compose logs --tail=100 api relay worker
```

### Start the Frontend

In a second terminal:

```bash
cd apps/web
npm ci
npm run dev
```

Open:

```text
http://localhost:5173
```

By default, enter the configured development bearer token in the dashboard.
To run the bounded anonymous experience, enable the backend with
`COMPUTEGRID_DEMO_MODE_ENABLED=true`, set a random
`COMPUTEGRID_DEMO_SESSION_SECRET` of at least 32 characters, and create
`apps/web/.env.local` from `apps/web/.env.example` with:

```dotenv
VITE_DEMO_MODE=true
```

The frontend then uses a signed, HttpOnly browser cookie and no longer asks the
visitor for a token. Keep `VITE_DEMO_MODE` aligned with the API setting. For a
cross-site HTTPS deployment, use secure cookies and an appropriate SameSite
policy; the local Compose and kind defaults use same-site HTTP development
settings. Use the same stable signing secret for every API replica; rotating it
intentionally invalidates existing anonymous sessions. Use the same hostname
(`localhost` or `127.0.0.1`) for both Vite and the API during local testing so
the browser applies the expected same-site cookie policy.

### Development Dependencies Only

For backend tests that require PostgreSQL and RabbitMQ:

```bash
make dev-deps-up
```

## 8. Testing and Continuous Integration

### Local Verification

Backend quality checks:

```bash
make backend-check
```

Individual test suites:

```bash
make test-integration
make test-fault
make test-e2e
```

Full regression verification:

```bash
make test-all
make check
```

Frontend verification:

```bash
make web-typecheck
make web-test
make web-build
```

The exact composition of aggregate targets is defined in the Makefile. Some targets may overlap.

### GitHub Actions

The CI workflow is defined in:

```text
.github/workflows/ci.yml
```

It runs automatically on pushes to `main` and on pull requests, using separate backend and frontend jobs.

**Backend CI**

- Python 3.12 environment.
- Ruff and mypy checks.
- Unit tests.
- PostgreSQL and RabbitMQ test services.
- Integration tests.
- Fault-injection tests.
- End-to-end tests.

**Frontend CI**

- Node.js environment.
- Dependency installation using `npm ci`.
- TypeScript type checking.
- Vitest tests.
- Production build.

The workflow uses isolated test services and disposable credentials. It does not deploy the application or require production secrets.

**Verified:** The first GitHub Actions CI run on October 30, 2026 completed successfully on commit `51c9755`.

- Backend CI: passed.
- Frontend CI: passed.
- Frontend Vitest: 18 tests passed.
- Total workflow duration: approximately 1 minute.

### Why GitHub Actions Instead of Jenkins?

GitHub Actions provides hosted CI execution and integrates directly with the GitHub repository.

Jenkins is also widely used, particularly where organizations require self-managed CI infrastructure and specialized build agents.

For this project's scale, maintaining a separate Jenkins controller and agents would add operational overhead without a clear benefit.

GitHub Actions is a project-specific engineering choice, not a claim that Jenkins is obsolete.

### CI vs CD

M3 implements **Continuous Integration**, not automated production deployment.

M4 established a reproducible manual deployment on local Kubernetes. Automated
delivery remains out of scope until M5 selects an affordable public hosting
environment with clear operational and rollback requirements.

## 9. Logging and Operational Diagnostics

### Structured Logging

ComputeGrid uses Python's standard logging library with a JSON formatter.

Application logs are written to stdout/stderr and collected through Docker.

Useful fields include:

- `timestamp`
- `severity`
- `service`
- `event`
- `message`
- `job_id`
- `attempt_number`
- `worker_id`

The Job ID links lifecycle events across components.

Example execution flow:

```text
API:
  job.submitted
      |
      v
Outbox Relay:
  job.queued
      |
      v
Celery Worker:
  job.claimed
      |
      v
Celery Worker:
  job.succeeded
```

**Verified:** A submitted job produced correlated events in the API, relay, and worker logs using the same `job_id`, including attempt and worker information.

To investigate a specific job:

```bash
JOB_ID="<job-uuid>"

docker compose logs --since=15m api relay worker \
  | grep -F "$JOB_ID"
```

The persisted job and attempt records remain authoritative. Logs provide diagnostic context rather than replacing durable state.

### Health and Readiness

ComputeGrid exposes two separate API probes.

**Liveness**

```text
GET /healthz
```

Checks whether the API process is responsive.

It does not require PostgreSQL connectivity.

**Readiness**

```text
GET /readyz
```

Checks whether the API can access its required PostgreSQL dependency.

Expected behavior:

| Condition | `/healthz` | `/readyz` |
| --- | --- | --- |
| API and DB healthy | 200 | 200 |
| PostgreSQL unavailable, API alive | 200 | 503 |
| PostgreSQL restored | 200 | 200 |

This behavior was verified by stopping PostgreSQL and restoring it.

Temporary database unavailability should not automatically be interpreted as a reason to restart an otherwise functioning API process.

### Backend Operations Summary

**M5A-1 status: Complete and verified in Docker Compose and Kubernetes**

M5A-1 provides an authenticated, read-only snapshot for the future live
operations console:

```text
GET /v1/operations/summary
```

The response uses actual dependency data:

- **RabbitMQ:** configured queue name, ready messages, unacknowledged messages,
  and consumer count from the management API.
- **Celery workers:** worker identity, availability, `IDLE` or `BUSY` activity,
  and the currently executing Job ID when the active task exposes one.
- **PostgreSQL:** authoritative job counts grouped by every durable job state.

Each dependency reports `AVAILABLE`, `UNAVAILABLE`, or `UNKNOWN`
independently. If RabbitMQ management, Celery inspection, or PostgreSQL is
temporarily unavailable, the endpoint returns a partial snapshot with null or
unavailable data rather than fabricated zero values or an unhandled failure.

With the development token loaded, query Docker Compose on port 8000 or the
Kubernetes port-forward on port 8001:

```bash
curl --fail \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  http://127.0.0.1:8000/v1/operations/summary

curl --fail \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  http://127.0.0.1:8001/v1/operations/summary
```

Remote RabbitMQ and Celery calls use the bounded
`COMPUTEGRID_OPERATIONS_TIMEOUT_SECONDS` setting. PostgreSQL statements and
connection establishment also have finite timeouts. The endpoint performs no
queue, worker, or job mutations and retains the existing development bearer
authentication boundary.

When anonymous demo mode is enabled, the same endpoint returns only safe
aggregates: queue counters, worker availability/idle/busy totals, and job counts
by state. It omits the RabbitMQ queue name, Celery worker identities, and active
Job IDs. Developer bearer authentication continues to return the full M5A-1
response. RabbitMQ and Celery failures retain the same graceful degradation
behavior. If PostgreSQL-backed abuse protection cannot be enforced, anonymous
requests fail closed with `503` rather than bypassing the limits.

Verification completed successfully in both supported environments:

- Docker Compose returned live RabbitMQ, Celery worker, and PostgreSQL data.
- The local kind deployment reported four RabbitMQ consumers and four Celery
  workers, all `AVAILABLE` and `IDLE` at the verification snapshot.
- Kubernetes returned PostgreSQL counts grouped by the persisted job states.
- Focused unit tests and PostgreSQL integration tests passed.
- `make backend-check` passed, including Ruff, strict mypy, and backend unit
  tests.

### Diagnostic Script

Run:

```bash
make doctor
```

The read-only diagnostic command inspects:

- Docker Compose service states.
- API liveness and readiness.
- PostgreSQL connectivity.
- RabbitMQ diagnostics.
- Queue depth and consumer count.
- Celery worker responsiveness.
- Relay process status.
- Recent application errors.

Example RabbitMQ information:

```text
name               ready   unacknowledged   consumers
computegrid.jobs     0           0              1
```

Here:

- `ready` means messages waiting for delivery.
- `unacknowledged` means messages delivered but not yet acknowledged.
- `consumers` means RabbitMQ consumers subscribed to the queue.

Consumer count is not the same as Celery process concurrency.

The tool reports uncertainty explicitly. For example, a running relay process can be reported as `UNKNOWN` for active health because no dedicated relay activity probe is implemented.

**Verified worker failure experiment:**

```text
Worker stopped:
  Container: UNHEALTHY / exited
  Process:   UNREACHABLE
  Consumers: 0

Worker restarted:
  Container: running
  Process:   Celery ping successful
  Consumers: 1
```

The API and PostgreSQL remained available during the worker interruption.

### Observability Scope

Dedicated monitoring and distributed tracing infrastructure is intentionally omitted.

Structured logs, persisted execution history, readiness checks, and lightweight diagnostics provide sufficient visibility for the current development scope.

Larger production environments may benefit from centralized logs, metrics, distributed tracing, alerting, and service-level objectives.

## 10. Horizontal Scaling and Queue-Based Load Distribution

ComputeGrid uses RabbitMQ's shared-queue model with competing Celery consumers.

Multiple workers can consume independent jobs concurrently.

```text
               RabbitMQ
               Job Queue
                   |
         +---------+---------+
         |         |         |
         v         v         v
      Worker 1  Worker 2  Worker 3
         |         |         |
       Job A     Job B     Job C
```

This is **job-level parallelism**.

Each computational job is independently evaluated by one worker execution. A single BQM or CQM problem is not partitioned across multiple workers.

### Load Distribution

RabbitMQ delivers messages to consumers according to consumer availability, delivery rules, and prefetch settings.

ComputeGrid does not implement a custom load balancer that measures per-worker CPU utilization or selects the least-loaded worker.

The current architecture supports:

- Multiple Celery worker instances.
- Multiple worker execution processes.
- Shared-queue task distribution.
- Horizontal worker scaling.
- Independent job execution.
- Lease-based recovery after worker failure.

Scaling improves available execution capacity when the workload and system resources permit it. Linear throughput gains are not guaranteed.

### M4C — Verified Kubernetes Worker Scaling

**M4C status: Complete**

M4C verified horizontal scaling of the Celery worker Deployment from one to two
and then four replicas in the local kind cluster.

The experiments confirmed:

- Kubernetes successfully created and scheduled the additional worker Pods.
- Each worker Pod reached a ready state.
- RabbitMQ reported two and four consumers as the corresponding worker replicas became available.
- A batch of 12 independent jobs was executed with two workers.
- A batch of 24 independent jobs was executed with four workers.
- Worker logs confirmed that multiple Pods independently claimed and completed jobs from the same shared queue.
- RabbitMQ competing-consumer distribution worked across the available workers.

Worker scaling can be performed with:

```bash
kubectl --context kind-computegrid -n computegrid \
  scale deployment/computegrid-worker --replicas=4
```

All worker Pods ran on the same single kind Node. This verifies Kubernetes
replica management and queue distribution, not additional physical compute
capacity. Throughput scaling for one, two, and four workers has not yet been
benchmarked; those measurements belong to M5.

### M4D — Verified Kubernetes Self-Healing and In-Flight Recovery

**M4D status: Complete**

M4D verified both Kubernetes-level workload management and ComputeGrid's
application-level recovery guarantees:

- Removing a worker Pod caused its Deployment to create a replacement Pod.
- A rolling restart completed successfully and replacement workloads returned
  to their ready state.
- An in-flight worker process was killed while it owned a `RUNNING` job;
  Kubernetes automatically restarted the failed container.
- ComputeGrid independently recovered the abandoned durable attempt after its
  lease expired and completed the job on another worker.

The verified in-flight experiment recorded:

| Field | Verified value |
| --- | --- |
| Job ID | `a2cc9b84-8fe2-43b5-a596-3c166a344437` |
| Workload | Monte Carlo Pi, 10,000,000 samples |
| Killed worker | `82cbv`, terminated with `SIGKILL` while the job was `RUNNING` |
| Kubernetes response | Automatically restarted the worker container |
| Temporary test lease | 30 seconds |
| Attempt 1 | `ABANDONED` with `WorkerLeaseExpired` |
| Attempt 2 | `SUCCEEDED` on worker `wphds` |
| Final Pi estimate | `3.1423096` |
| End-to-end recovery | Approximately 33.58 seconds |

The 30-second lease was used only to make the failure exercise practical. The
normal configured lease remains 300 seconds. Kubernetes restart and
ComputeGrid recovery are complementary: Kubernetes restores the worker
process, while the durable lease, retry policy, and fencing token recover the
job and prevent a stale attempt from overwriting the authoritative result.

This remains an at-least-once execution model. Physical work can overlap or be
performed more than once after a failure even though guarded durable state
transitions prevent more than one authoritative result.

## 11. Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| M1 | REST API, PostgreSQL, React, Docker Compose | Complete |
| M2A | RabbitMQ, Celery, transactional outbox | Complete |
| M2B | Bounded retries and terminal failures | Complete |
| M2C | Worker leases, crash recovery, fencing | Complete |
| M2.5 | Weighted Max-Cut and dashboard improvements | Complete |
| M2.6 | Generic BQM and restricted CQM | Complete |
| M3 | GitHub Actions CI and lightweight operations | Complete |
| M4A | Local single-node kind environment | Complete |
| M4B | Kubernetes backend deployment and end-to-end execution | Complete |
| M4C | Manual worker scaling and queue distribution | Complete |
| M4D | Pod self-healing, rolling restart, and in-flight recovery | Complete |
| M5 | Live operations console, real metrics, load benchmarks, recovery visualization, affordable public demo | In progress |
| M5A-1 | Authenticated backend observability API | Complete |
| M5A-2 | Isolated anonymous demo access and abuse protections | Complete |
| M5B | Animated live Queue → Worker → Completion console | Implemented; verification pending |
| M5C | Backend-controlled Computational Load Demo | Implemented; verification pending |
| M5E-A | Reproducible authenticated job performance harness | Complete |
| M5E-D | CPU scaling bottleneck diagnostics | Complete |

### M5 — Interactive Demo and Reliability Evidence

M5 is in progress. M5A-1 and M5A-2 are complete and verified. M5B is implemented
and awaiting local verification; later work will add controlled load,
browser-triggered failure demonstrations, and performance evidence.

- A live operations console.
- Actual RabbitMQ queue and worker metrics.
- Controlled load generation and reproducible throughput benchmarks.
- Failure-recovery visualization based on persisted state.
- An affordable public demo deployment with bounded resource and access costs.

#### M5B Animated Live Operations Console

The dashboard places a compact execution flow immediately above Recent Jobs:

```text
visitor-visible queued Jobs → actual discovered Workers → completion
```

RabbitMQ provides real ready, unacknowledged, consumer, and availability
metrics. Celery inspection provides the discovered worker set and its actual
`IDLE`, `BUSY`, `ONLINE`, or `UNKNOWN` state. PostgreSQL provides authoritative
job-state counts plus the current visitor's persisted Jobs, latest attempt
state, and durable worker claim. Queue depth remains a system-wide aggregate;
the UI never converts that count into invented Job blocks.

Job blocks use stable Job IDs and restrained semantic borders: accepted/queued
is blue, running and retry wait are amber, succeeded is green, failed/cancelled
is red, and an abandoned latest attempt is marked interrupted. At most six
blocks appear in a lane, with a real `+N more` count for additional tracked
Jobs. Four cards appear in one desktop row when four workers are actually
discovered; no placeholder workers are fabricated.

Observed state changes animate through the Queue, Worker, Retry Wait, and
Completion areas. A `RUNNING → RETRY_WAIT` observation moves the same block out
of its persisted worker and marks an abandoned attempt as interrupted. It does
not return to Queue until PostgreSQL reports `QUEUED`, and it moves to a new
worker only after a new persisted running claim identifies that worker.
Terminal transitions briefly move downward toward Recent Jobs and highlight
the existing ledger row without inserting a duplicate.

The console polls approximately every three seconds with one request in flight
at a time. A failed refresh preserves the last successful snapshot and marks it
stale. Partial RabbitMQ, Celery, or PostgreSQL failures remain visibly distinct
from valid zero counts. Polling and pending requests are cleaned up on unmount.
View Transitions provide short layout movement when supported; CSS fallbacks
remain functional, and `prefers-reduced-motion` disables motion.

Developer bearer access can correlate authorized Jobs with raw worker records.
Anonymous sessions receive only their own Jobs and stable HMAC-derived worker
keys and labels. Raw Celery names, Pod names, other visitors' Job IDs, and
restricted infrastructure details are never returned to anonymous clients. If
a persisted running Job cannot be matched to a currently discovered worker, it
appears in an explicit unassigned execution area rather than on a guessed card.

Polling can miss transitions shorter than the refresh interval. The UI animates
only consecutive states it actually observes; it does not synthesize skipped
Queue, retry, failure, or completion events. Docker Compose and Kubernetes use
the same API and correlation rules.

#### M5A-2 Anonymous Demo Access

Anonymous mode is opt-in and preserves developer bearer authentication. The
API issues a random, signed, HttpOnly session cookie and maps it to an opaque
client identifier. Existing repository filtering then limits job lists,
details, results, and attempt history to the browser session that created them;
unknown jobs return `404` rather than revealing another visitor's records.

Abuse controls are enforced on the server:

- PostgreSQL-backed fixed-window submission, read, operations, and global
  request limits work across API processes.
- Per-visitor and global active- and retained-job admission limits are checked
  under a PostgreSQL advisory transaction lock. Expired rate-counter rows are
  removed opportunistically.
- Anonymous Monte Carlo sample counts and optimization variable counts are
  stricter than the already bounded developer API schemas.
- The operations endpoint exposes aggregate capacity only. It never grants
  infrastructure-control operations or reveals developer credentials.

The main settings are documented in `.env.example`. Compose passes them to the
API, while Kubernetes stores the session-signing secret in
`computegrid-secrets` and the non-sensitive limits in `computegrid-config`.
`scripts/k8s-create-secret.sh` generates or safely adds the signing secret;
set `COMPUTEGRID_DEMO_MODE_ENABLED` to `"true"` in the ConfigMap only when the
anonymous deployment is intended.

For direct API use, let the client retain the HttpOnly cookie. This example
creates a private cookie jar and lists only that anonymous session's jobs:

```bash
curl --fail --cookie-jar /tmp/computegrid-demo.cookies \
  --cookie /tmp/computegrid-demo.cookies \
  http://127.0.0.1:8000/v1/jobs
```

Use the same cookie options for submission, detail, and safe operations-summary
requests. Do not place the cookie value in URLs or logs.

This is bounded portfolio-demo access, not user authentication. The cookie is
the visitor's only proof of ownership: clearing or losing it loses access to
that session's jobs, and copying it transfers that access until expiry. Rate
limits reduce abuse but do not replace an edge proxy, network-level denial-of-
service protection, moderation, retention policies, or production identity
management.

Local verification commands for anonymous access and the M5B console:

```bash
make backend-check
make test-integration
make test-all
make web-check
```

Vitest uses the committed `apps/web/.env.test`, which fixes the test API URL at
`http://localhost:8000` and disables Demo Mode. Local Kubernetes or demo values
in `apps/web/.env.local` therefore continue to affect development only, while
tests that need anonymous mode enable it explicitly through component inputs.

#### M5C — Computational Load Demo

`Run Compute Demo` starts one server-controlled feed in the existing operations
console. The four editable manual forms retain their small examples unchanged.
The feed submits real jobs for up to 20 seconds; job execution may continue
afterward. The UI distinguishes submission countdown, feed complete/processing,
and execution complete (all jobs terminal, including failures).

`POST /v1/demo-runs` requires the existing authenticated client/session and an
`Idempotency-Key`. It accepts no workload configuration. `GET /v1/demo-runs/latest`
restores the visitor's latest run after refresh; `GET /v1/demo-runs/{id}` returns
only owned runs. Progress reports database-derived submitted and per-state
counts, start/deadline timestamps, remaining feed time, and throttling reasons.

The existing outbox relay advances PostgreSQL feed cursors. Under the same
advisory admission lock used for anonymous jobs, it atomically writes each job,
its normal acceptance outbox event, and the feed cursor. IDs and idempotency
keys derive from run ID and sequence. Duplicate starts return the same run;
concurrent relays cannot duplicate a sequence. Restarting resumes only before
the persisted deadline, without a catch-up burst or deadline extension. API or
browser restarts do not own scheduling. No separate queue, workers, or timers
inside workers are introduced. Apply migrations through
`0005_demo_run_bounds` before restarting the API and relay.

Presets rotate deterministically through:

- Monte Carlo π: 10,000,000 samples, seed 42.
- Max-Cut: dense 18-node graph, 153 deterministic positive-weight edges.
- BQM: 18 binary variables, 18 linear biases, and 153 signed interactions.
- Restricted CQM: 18 binary variables, an 18-coefficient linear objective, and 18
  resource constraints with 18 coefficients each.

These use the existing solvers and validation maxima. Enumeration checks at
most 262,144 assignments (Max-Cut evaluates one representative per complement).
No sleep or artificial computation is added. Some models may complete in less
than a second. A 2–5 second runtime or queue buildup is **not guaranteed**;
measure on the target CPU, with the intended worker concurrency and limits.

The computational feed submits at most one job every 0.5 seconds for the
unchanged 20-second window, with a maximum of 40 jobs per run. Only one feed
may be active, and the global active-job capacity remains 40. The relay polls
at the same 0.5-second cadence, so each due tick can admit one job without
catch-up bursts.

Settings use the `COMPUTEGRID_COMPUTE_DEMO_` prefix and are wired into Compose
and the kind API/relay through the ConfigMap:

| Suffix | Default | Purpose |
| --- | --- | --- |
| `INTERVAL_SECONDS` | 2 | At most one submission per due feed tick; minimum 1 |
| `MAX_JOBS` | 10 | Per-run ceiling, hard maximum 20 |
| `MAX_FEEDS` | 1 | Simultaneous global feeds, hard maximum 4 |
| `ACTIVE_CAPACITY` | 40 | Stop adding jobs while system-wide nonterminal count reaches cap |
| `RETAINED_CAPACITY` | 5000 | Stop admission when retained job count reaches cap |
| `COOLDOWN_SECONDS` | 120 | Per-client persisted start cooldown; minimum 20 |
| `SAMPLES` | 10000000 | Monte Carlo preset size, existing maximum 10 million |
| `VARIABLES` | 16 | Optimization preset size, range 4–16 |

Rate limits additionally bound starts to five/minute per client and reads to
60/minute, with 240/minute shared demo endpoint requests. Limits apply to
developers as well as anonymous sessions. Browser POSTs require an allowed
Origin; anonymous POSTs without Origin are rejected. Cross-visitor reads return
404. Generated jobs retain the requester's client ID and use the existing M5B
privacy/correlation rules. Capacity throttling skips a tick and records why;
the deadline does not move. Historical throttling reasons remain visible.
Retained run records share the same numeric ceiling (counted independently),
so failed/empty feeds cannot grow metadata without bound. Completed jobs and
run records are not automatically deleted. Ordinary anonymous manual job
limits remain in force; only server-selected presets use separate admission.

Manual verification (not executed during implementation):

```bash
make dev-deps-up
make migrate
make test-all
make check
make dev-up
make web-dev
```

For kind, update desired non-secret settings in `k8s/10-configmap.yaml`, then:

```bash
make k8s-image
make k8s-configure
make k8s-migrate
make k8s-apps
make k8s-port-forward
VITE_API_BASE_URL=http://localhost:8001 VITE_DEMO_MODE=true npm --prefix apps/web run dev
```

Click once, refresh mid-feed, and confirm submitted counts resume from storage.
After the deadline confirm counts stop increasing while jobs continue to
complete. A second browser session must not see the first session's run or jobs.
Observe real RabbitMQ metrics and worker assignments; fast states can still be
missed by polling. The run display polls every three seconds; the local
countdown interpolates the server's remaining time and does not submit jobs.
API outages retain last known progress and show an error.

Benchmark each actual preset once without database or broker dependencies:

```bash
.venv/bin/python scripts/benchmark_compute_demo.py
```

Repeat manually on the target hardware and record CPU, worker concurrency,
container CPU/memory limits, preset sizes, wall times, and lease duration. Also
measure persisted attempt start/finish durations under an actual feed. Keep
worst-case execution comfortably below the normal 300-second lease. Tune
`SAMPLES`/`VARIABLES` downward if necessary, then tune interval and job caps for
the measured capacity. Never raise enumeration beyond the current solver
bounds just to make an animation visible. Public deployment requires this
calibration and remains out of scope.

#### Worker Failure Demonstration

For local development only, provide a controlled way to terminate a selected worker.

Visualize the actual sequence:

```text
Worker executing Job
         |
         v
Worker terminated
         |
         v
Execution lease expires
         |
         v
Attempt marked ABANDONED
         |
         v
Job scheduled for retry
         |
         v
Another Worker claims Job
         |
         v
Job completes
```

A killed worker's job must not be shown as immediately requeued unless that transition has actually occurred.

The visualization must reflect durable attempt state and real recovery timing.

Any infrastructure-control endpoint must be restricted to a local or explicitly protected demo environment. Do not expose Docker control access through a public API.

#### M5E-A Reproducible Performance Benchmark

The standalone `scripts/benchmark_jobs.py` client submits a fixed batch of
authenticated `monte_carlo_pi` jobs through `POST /v1/jobs`. Every request uses
a unique `m5e-a:<run-id>:<index>` idempotency key. The client polls each job's
authenticated detail endpoint until it reaches `SUCCEEDED`, `FAILED`, or
`CANCELLED`, or until the explicit benchmark timeout expires.

Metrics use authoritative server timestamps rather than the benchmark host's
wall clock:

- Queue latency: `started_at - created_at`. This includes transactional outbox
  and broker wait time before a worker starts execution.
- Execution latency: `completed_at - started_at`.
- End-to-end latency: `completed_at - created_at`.
- Throughput: terminal jobs divided by the interval from the earliest
  `created_at` to the latest `completed_at`.
- Failure rate: `FAILED` plus `CANCELLED` jobs divided by terminal jobs.
- p50 and p95 use deterministic linear interpolation (Hyndman-Fan type 7).

Metrics with missing timestamps exclude that job and report their sample
count. Submission errors, polling timeouts, non-terminal jobs, and job errors
remain visible in the output. The command returns exit status 2 when any
submission fails or any accepted job is still non-terminal at the timeout.
Terminal job failures are valid benchmark results and are reported through
`failure_rate`.

The JSON output contains configuration, environment metadata, summary metrics,
submission errors, and per-job records. The CSV contains one row per job or
failed submission, per-job latencies, and repeated flattened summary metrics.
The bearer token is never written to either output.

Install the development environment and start the complete Compose stack:

```bash
make install
make dev-up
export COMPUTEGRID_DEVELOPMENT_API_TOKEN='the value configured in .env'
```

Use the same job count, sample count, submission concurrency, poll interval,
and Celery concurrency for every comparison. The following sequence fixes
Celery concurrency at one process per replica and varies only replica count:

```bash
# Run 1: one worker replica
COMPUTEGRID_WORKER_CONCURRENCY=1 docker compose up -d --no-deps \
  --scale worker=1 --force-recreate worker
docker compose ps worker
.venv/bin/python scripts/benchmark_jobs.py \
  --jobs 40 --samples 10000000 --submission-concurrency 8 \
  --worker-replicas 1 --celery-concurrency 1 \
  --cpu-limit none-compose --memory-limit none-compose \
  --output-prefix benchmark-results/m5e-a-workers-1

# Run 2: two worker replicas
COMPUTEGRID_WORKER_CONCURRENCY=1 docker compose up -d --no-deps \
  --scale worker=2 --force-recreate worker
docker compose ps worker
.venv/bin/python scripts/benchmark_jobs.py \
  --jobs 40 --samples 10000000 --submission-concurrency 8 \
  --worker-replicas 2 --celery-concurrency 1 \
  --cpu-limit none-compose --memory-limit none-compose \
  --output-prefix benchmark-results/m5e-a-workers-2

# Run 3: four worker replicas
COMPUTEGRID_WORKER_CONCURRENCY=1 docker compose up -d --no-deps \
  --scale worker=4 --force-recreate worker
docker compose ps worker
.venv/bin/python scripts/benchmark_jobs.py \
  --jobs 40 --samples 10000000 --submission-concurrency 8 \
  --worker-replicas 4 --celery-concurrency 1 \
  --cpu-limit none-compose --memory-limit none-compose \
  --output-prefix benchmark-results/m5e-a-workers-4
```

Wait for the worker replicas to be running and for any earlier queue backlog or
Run Demo feed to finish before each run. Repeat each configuration enough times
to distinguish stable behavior from noise; do not compare a warm run with a
cold deployment without recording that distinction.

Compose currently imposes no worker CPU or memory limit. All worker containers
share the Docker host's allocated CPUs and memory, so record Docker Desktop or
host CPU model, CPU count, memory allocation, power mode, and competing load
alongside the generated files. Adding replicas on one host does not add
physical CPU. The kind manifest is a different environment: each worker Pod
requests 100m CPU/128 MiB memory and is limited to 1 CPU/512 MiB, while all Pods
still share the single kind node. Do not combine Compose and kind measurements
in one scaling comparison.

Additional options include `--timeout`, `--poll-interval`, `--seed`,
`--api-url`, and `--run-id`; run the script with `--help` for the complete
interface. Do not invent benchmark results or assume linear scaling.

#### M5E-D CPU Scaling Bottleneck Diagnostics

The standalone `scripts/benchmark_cpu_scaling.py` benchmark calls the exact
production `monte_carlo_pi` function without a database, broker, Celery, or HTTP
server. It does not copy, optimize, vectorize, or otherwise rewrite the
computation. Each child process receives the same fixed sample count and seed.

By default it runs equal batches with 1, 2, and 4 process-pool workers and
reports:

- Batch wall-clock time, including process-pool dispatch and lifecycle cost.
- Summed child-process CPU time and coordinator-process CPU time.
- Median per-job pure-computation wall and CPU time measured immediately around
  the production function call inside each child.
- Job and sample throughput.
- Speedup relative to one process.
- Scaling efficiency: `speedup / process_count`.
- Average CPU cores used: total process CPU time divided by wall time.

Run the CPU-only diagnostic on an otherwise idle target host:

```bash
.venv/bin/python scripts/benchmark_cpu_scaling.py \
  --processes 1,2,4 \
  --jobs 8 \
  --repeats 3 \
  --samples 10000000 \
  --seed 42 \
  --output-prefix benchmark-results/m5e-d-cpu-scaling
```

The command writes JSON with platform and logical-CPU metadata plus CSV with
one row per process count. Keep CPU affinity, Docker Desktop CPU allocation,
power mode, thermal state, and competing host load fixed. Run it multiple times
and retain every result rather than selecting the best run. If the host has
fewer than four physical cores, the four-process result diagnoses contention;
it does not represent four independent machines.

Optional worker phase timing is disabled by default. `execute_job` accepts a
timing sink that receives claim, pure computation, finalize, persistence
(`claim + finalize`), non-computation overhead, and total wall times. For a
temporary Compose diagnostic worker, first stop the normal workers, then run:

```bash
docker compose stop worker
COMPUTEGRID_WORKER_CONCURRENCY=1 docker compose run --rm --no-deps \
  -e COMPUTEGRID_EXECUTION_TIMING_ENABLED=true worker
```

In another terminal, submit an M5E-A batch. The temporary worker emits
`job.execution_timing` JSON log records. Stop it after the batch and restore the
normal worker replicas. The timing hook only observes phase boundaries; it does
not change computation inputs, retry rules, state transitions, or results.

To measure observer overhead, run identical M5E-A batches against the same
worker configuration at both polling cadences:

```bash
.venv/bin/python scripts/benchmark_jobs.py \
  --jobs 40 --samples 10000000 --submission-concurrency 8 \
  --poll-interval 0.5 --worker-replicas 1 --celery-concurrency 1 \
  --output-prefix benchmark-results/m5e-d-poll-0.5

.venv/bin/python scripts/benchmark_jobs.py \
  --jobs 40 --samples 10000000 --submission-concurrency 8 \
  --poll-interval 2.0 --worker-replicas 1 --celery-concurrency 1 \
  --output-prefix benchmark-results/m5e-d-poll-2.0
```

Compare `metrics.polling.request_count`, `request_failures`,
`aggregate_http_seconds`, and `polling_wall_seconds` in the JSON files. The
server-timestamp latency and throughput metrics are intentionally unaffected by
how soon the client observes completion; a longer polling interval can increase
the benchmark client's wall time while reducing GET request load. Use distinct
output prefixes and the default unique run IDs so the two batches do not reuse
idempotency keys.

#### Public Demonstration

Publish a limited live demo if practical.

Requirements include:

- Workload and payload limits.
- Submission-rate controls.
- Appropriate authentication and access restrictions.
- No arbitrary executable job payloads.
- No publicly accessible worker-kill or infrastructure-control endpoints.
- Resource and cost limits.

The public hosting strategy does not need to use EKS if a simpler and cheaper solution meets the requirements.

## 12. Deliberately Out of Scope

ComputeGrid favors engineering decisions supported by real requirements over collecting infrastructure technologies.

| Tool or capability | Decision |
| --- | --- |
| Jenkins | Not deployed; GitHub Actions provides sufficient CI |
| Prometheus / Grafana | Not required for current-scale monitoring |
| OpenTelemetry | Not required without a meaningful distributed tracing need |
| Helm | Optional Kubernetes packaging exercise |
| KEDA | Not required; manual worker scaling is sufficient for initial experiments |
| Argo CD | No GitOps controller needed for this small deployment |
| Terraform | Not necessary for local Kubernetes or a short-lived cloud experiment |
| Redis | No caching requirement currently identified |
| Custom load-balancing scheduler | RabbitMQ competing consumers meet current needs |
| Distributed intra-job optimization | Each job executes independently on one worker |
| Quantum solver integration | Not required; classical optimization workloads are sufficient |
| Large benchmarking stack | Small reproducible experiments are sufficient |

These are scope decisions, not claims that the tools are unsuitable for larger production systems.

## 13. Security and Operational Limitations

ComputeGrid is an engineering portfolio and learning project, not a production-ready multi-tenant compute platform.

Important boundaries:

- Developer bearer tokens and anonymous signed-cookie sessions are not
  production-grade identity management.
- API idempotency and worker message deduplication solve different problems.
- At-least-once delivery does not imply exactly-once physical execution.
- Fixed execution leases require bounded workloads.
- External side effects need additional idempotency safeguards.
- PostgreSQL and RabbitMQ must not be exposed directly to the public internet.
- Credentials must never be committed to Git.
- Anonymous demo limits are deliberately conservative but do not make an
  internet deployment immune to distributed abuse.
- Public infrastructure-control operations must be disabled or strongly isolated.
- Database backup, disaster recovery, production observability, and operational hardening remain outside the current scope.

Performance and reliability claims should be supported by actual measurements and documented experiments.

## 14. Repository Structure

```text
.github/workflows/
    ci.yml                  GitHub Actions CI

apps/api/computegrid_api/
    main.py                 FastAPI application
    celery_app.py           Celery configuration
    outbox_relay.py         Event publication and recovery
    logging_config.py       Structured JSON logging
    jobs/                   Models, execution, optimization,
                            persistence, and recovery

apps/web/
    src/                    React dashboard and tests

migrations/                 Alembic migrations

scripts/
    doctor.sh               Read-only diagnostics
    k8s-create-secret.sh    Ignored local credential generation

k8s/                       Plain local Kubernetes manifests

tests/
    unit/                   Unit tests
    integration/            Database and service integration tests
    fault/                  Fault-injection tests
    e2e/                    End-to-end tests

compose.yaml                Docker Compose services
Makefile                    Development and verification commands
.env.example                Local environment configuration template
README.md                   Project documentation
```

## 15. Project Summary

ComputeGrid demonstrates the development of a fault-tolerant distributed computation service with durable state, message-driven execution, bounded retries, worker crash recovery, and automated operational verification.

The implemented platform includes:

- Transactional outbox messaging.
- RabbitMQ and Celery job distribution.
- Persistent execution attempts.
- Lease-based recovery and stale-result fencing.
- Classical optimization workloads.
- GitHub Actions CI.
- Structured lifecycle logging.
- Health and readiness probes.
- Operational diagnostics.
- An authenticated, read-only operations summary backed by live RabbitMQ,
  Celery worker, and PostgreSQL data.
- Optional anonymous browser sessions with per-visitor job isolation, safe
  aggregate operations data, and shared abuse controls.
- A verified full-stack deployment on local kind Kubernetes.
- Manual worker scaling with RabbitMQ competing-consumer distribution.
- Kubernetes Pod self-healing and successful rolling restart behavior.
- Verified in-flight worker failure recovery using durable leases and fencing.

M5A-1 and M5A-2 are complete and verified. M5B's animated console is
implemented and awaiting local verification, using real RabbitMQ, Celery
worker, persisted attempt, and PostgreSQL job data. M5C computational feeds
are implemented pending verification. Later failure controls, throughput benchmarks, and an affordable
public demo remain future work. The overall M5 milestone remains in progress.

The central objective is to demonstrate **distributed-systems correctness, automated validation, and practical cloud operations through a small, explainable system**.
