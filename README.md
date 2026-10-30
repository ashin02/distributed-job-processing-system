# ComputeGrid / Fault-Tolerant Distributed Job Processing System

**A fault-tolerant Python job-processing service: REST API, durable queues, distributed workers, and Kubernetes autoscaling.**

> **Status:** Milestone 3 is implemented for local development: CI, structured logs, dependency-aware readiness, and read-only diagnostics now complement the M1–M2.6 application. Kubernetes, an optional AWS EKS practice environment, and the public operations demo remain on the roadmap.
>
> **Context:** Inspired by general engineering challenges in cloud-hosted compute services. This is an independent learning project, not a D-Wave product, implementation, or affiliation.

## Current implementation: Milestone 3

The repository contains the FastAPI application, PostgreSQL persistence, authenticated job APIs, a transactional outbox relay, RabbitMQ transport, Celery workers, and a React/TypeScript operations dashboard. New jobs and their outbox events are committed atomically, client-scoped idempotency is enforced by PostgreSQL, and four bounded workloads execute asynchronously: deterministic `monte_carlo_pi`, `weighted_max_cut`, generic `bqm`, and restricted `cqm`. All optimization uses exact classical CPU enumeration. Weighted Max-Cut compiles to a shared BINARY BQM solver; generic BQMs support QUBO and Ising domains; restricted CQMs support binary variables with linear objectives and constraints. This is an educational optimization service, not a quantum computer emulator or D-Wave Hybrid Solver replacement. Existing publisher-confirm, retry, lease recovery, fencing, and duplicate-delivery guarantees apply unchanged to every job type.

M3 adds separate backend and frontend GitHub Actions jobs, JSON application logs,
an API readiness probe backed by PostgreSQL, a Compose API health check, and a
read-only `make doctor` command. These operational additions do not change job
execution, retry, lease, or fencing semantics.

M2.5 was confirmed with 12 passing frontend tests, TypeScript type checking, a
production Vite build, and a live Max-Cut job whose optimal cut weight and QUBO
energy were 5 and -5. The M2.6 backend suite, Ruff, and mypy were subsequently
confirmed locally. The corrected frontend mock-response regression and the new
M3 changes should be verified with the commands below.

### Local setup

Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 20.19 or newer, and npm are required. Create the project-local Python environment and install the frontend dependencies:

```bash
make install-all
```

This installs Python 3.12 through uv when it is not already available, creates `.venv`, and installs both runtime and development dependencies. Make targets invoke `.venv/bin/python` directly, so activating the virtual environment is optional.

Create local configuration and replace the example password with a development-only value:

```bash
cp .env.example .env
```

Also replace `COMPUTEGRID_DEVELOPMENT_API_TOKEN` with a random local token. This single-token authentication mode is for development only; its configured client ID owns every job submitted with that token.

Start PostgreSQL and RabbitMQ, then apply the schema migration when running processes directly on the host:

```bash
make dev-deps-up
make migrate
```

On first initialization PostgreSQL creates separate `computegrid` and `computegrid_test` databases. PostgreSQL and RabbitMQ data are retained in named volumes. Application settings use the `COMPUTEGRID_` environment prefix and are read from the process environment or `.env`.

The simplest full-stack backend start builds the application image, migrates PostgreSQL, and starts the API, relay, RabbitMQ, and Celery worker:

```bash
make dev-up
```

Follow backend logs and stop the stack with:

```bash
make dev-logs
make dev-down
```

For direct host-process development, run `make run`, `make relay`, and `make worker` in separate terminals after `make dev-deps-up && make migrate`.

Start the Vite dashboard in another terminal:

```bash
make web-dev
```

Open `http://127.0.0.1:5173`, paste the development API token from `.env`, and connect. The token is retained only in browser session storage. Four selectable job cards provide examples and expandable educational explanations. Weighted Max-Cut results include an SVG graph, highlighted partition, and QUBO coefficients; BQM and CQM results show assignments, energy or objective values, feasibility, constraint evaluations, and solver metadata. A selected active job refreshes automatically every second, and its execution timeline retains persisted lifecycle timestamps, durations, retries, abandoned attempts, results, and errors after completion. Set `VITE_API_BASE_URL` in `apps/web/.env` when the API is not available at `http://localhost:8000`.

FastAPI allows the local Vite origins configured by `COMPUTEGRID_CORS_ORIGINS`. The value is a JSON array; keep production origins out of this development configuration.

The API is available at `http://127.0.0.1:8000`. Liveness checks only that the
process can answer HTTP; readiness also executes `SELECT 1` through the normal
PostgreSQL session factory:

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

`/readyz` returns `503 Service Unavailable` when PostgreSQL cannot be reached.
RabbitMQ is intentionally excluded: a temporary broker outage delays queueing
but does not make the API process dead or prevent PostgreSQL-backed job reads.
Compose uses the existing PostgreSQL and RabbitMQ health checks and probes the
API through `/readyz`.

For a concise, read-only view of the local stack, run:

```bash
make doctor
```

The doctor reports Compose container state, API liveness/readiness, PostgreSQL
and RabbitMQ probes, RabbitMQ ready/unacknowledged message counts and consumers,
Celery ping status, relay process status, and recent error-level application
logs. It labels results as healthy, unhealthy, unreachable, or unknown and
never restarts services, changes queues, or writes to the database. Set
`COMPUTEGRID_API_URL` when diagnosing an API exposed at a non-default address.

Submit and inspect an asynchronously executing job using the token configured in `.env`:

```bash
set -a
source .env
set +a

curl --fail-with-body \
  -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-001' \
  -d '{"type":"monte_carlo_pi","parameters":{"samples":1000000,"seed":42}}'

curl --fail-with-body \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  'http://127.0.0.1:8000/v1/jobs?limit=20&offset=0'
```

Submit a bounded Weighted Max-Cut graph with 2–16 uniquely labeled nodes, unique
undirected edges, and positive finite edge weights:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer $COMPUTEGRID_DEVELOPMENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: max-cut-example-001' \
  -d '{"type":"weighted_max_cut","parameters":{"nodes":["a","b","c"],"edges":[{"source":"a","target":"b","weight":2},{"source":"b","target":"c","weight":3},{"source":"a","target":"c","weight":1}]}}'
```

The solver minimizes the upper-triangular QUBO
`-Σ w(u,v)(x_u + x_v - 2x_u x_v)`, whose energy is the negative cut weight.
It fixes the first variable to one side to remove complemented duplicates and
enumerates at most `2^15` assignments. Results include the optimal partition,
cut weight, QUBO coefficients, and exact-solver metadata.

### Supported optimization model boundary

| Job type | Model | Exact local behavior | Boundary |
| --- | --- | --- | --- |
| `weighted_max_cut` | Positive weighted graph | Converts Max-Cut to a BINARY QUBO/BQM and minimizes `-cut_weight` | 2–16 nodes; unique undirected edges |
| `bqm` | BINARY QUBO or SPIN Ising model | Minimizes offset plus linear and quadratic biases | 1–16 named variables; at most 120 unique interactions |
| `cqm` | Restricted constrained binary model | Enumerates feasible assignments and minimizes or maximizes a linear objective | 1–16 variables; 1–32 linear `<=`, `>=`, or `==` constraints |

A QUBO is a BQM whose variables take values in `{0, 1}`. An Ising model is a
BQM whose variables take values in `{-1, +1}`. Weighted Max-Cut is one concrete
problem that can be translated into a QUBO. A CQM adds explicit constraints;
ComputeGrid supports only the deliberately narrow binary/linear subset above,
not the complete D-Wave CQM feature set.

Every model is solved by bounded exhaustive enumeration on ordinary CPU workers.
There is no quantum SDK, QPU access, embedding, sampling service, or remote
D-Wave Hybrid Solver integration. If a CQM has no feasible assignment, the job
still completes successfully with `status: INFEASIBLE`, `feasible: false`, and
no assignment or objective value. This distinguishes model infeasibility from
worker or infrastructure failure.

Example generic BQM request parameters:

```json
{
  "vartype": "BINARY",
  "variables": ["x", "y"],
  "linear": {"x": -1, "y": -1},
  "quadratic": [{"source": "x", "target": "y", "bias": 2}],
  "offset": 0
}
```

Example restricted CQM request parameters:

```json
{
  "variables": ["x", "y"],
  "objective": {"sense": "maximize", "linear": {"x": 3, "y": 2}},
  "constraints": [
    {"name": "choose-one", "linear": {"x": 1, "y": 1}, "sense": "<=", "rhs": 1}
  ]
}
```

`POST /v1/jobs` returns `202 Accepted` only after the job and `job.accepted` outbox event commit together. The relay later publishes the event and moves the job to `QUEUED`; a worker records a fenced attempt and moves it through `RUNNING` to `SUCCEEDED`, `RETRY_WAIT`, or `FAILED`. Due retry events return the job to `QUEUED` for the next numbered attempt. An identical retry with the same client-scoped `Idempotency-Key` returns the original job reference; a different validated payload with that key returns `409 Conflict`. `GET /v1/jobs/{job_id}` and the paginated `GET /v1/jobs` expose only jobs owned by the authenticated development client.

Run the complete local quality suite, or individual checks:

```bash
make check
make test
make test-integration
make test-fault
make test-e2e
make test-all
make lint
make typecheck
make web-test
make web-typecheck
make web-build
```

`make check` runs backend linting, strict typing and unit tests followed by frontend type checking, tests, and a production build. `make test` runs database-independent backend unit tests. `make test-integration` and `make test-fault` require Compose PostgreSQL and use only `COMPUTEGRID_TEST_DATABASE_URL`; the database name must end in `_test`. Fault tests pause an active execution, expire its lease, recover it, and verify stale fencing. `make test-e2e` additionally requires RabbitMQ at `COMPUTEGRID_TEST_BROKER_URL` and exercises submission, confirmed relay publication, Celery execution, durable results, and duplicate delivery. Migration tests rebuild the dedicated test schema, so never point this variable at a database containing data you need to retain.

### M2C reliability boundary

- The relay locks pending outbox rows with `FOR UPDATE SKIP LOCKED`, publishes with RabbitMQ confirms enabled, and marks an event published only after the publish call succeeds. A crash after broker confirmation but before the database commit can publish a duplicate by design.
- Celery uses late acknowledgement, rejects on worker loss, and prefetches one job per worker slot. A worker creates a fenced attempt before computation and returns normally only after success, terminal failure, or a durable retry event is committed.
- `RetryableExecutionError` identifies failures eligible for retry; other computation exceptions are permanent. Retry settings control maximum attempts, base/max delay, and proportional jitter. Exhausted failures retain attempt-limit metadata for inspection.
- Retry timing is durable PostgreSQL state: the failed attempt, `RETRY_WAIT`, and a future `job.retry_scheduled` outbox event commit atomically. The relay cannot publish that event before `next_attempt_at`.
- Messages carry their expected attempt number. A stale delivery superseded by a later durable generation is an idempotent no-op, while a duplicate for the currently running generation is requeued until completion.
- Each claim persists a bounded execution lease. The relay scans expired `RUNNING` attempts, locks the authoritative job and attempt, marks the attempt `ABANDONED`, advances the job fencing generation, and applies the same retry/exhaustion policy as other retryable failures.
- Success and failure commits require the claim's fencing token. A worker that resumes after recovery receives a stale-claim error and cannot replace the recovered job state or result; its redelivered message is later acknowledged as superseded.
- Compose sends `SIGTERM` and gives Celery a bounded soft-shutdown window. Late acknowledgements and worker-loss rejection preserve in-flight messages when a worker cannot finish gracefully.
- Kubernetes deployment and autoscaling remain future work.

## Goal

Build a small cloud computing service that can **accept, track, execute, retry, and observe compute jobs** while surviving worker failures. The primary objective is to demonstrate practical Python backend engineering, Linux-based operations, testing, and distributed-systems correctness. An accompanying repository, **CloudForge**, may provision an optional AWS EKS environment later.

### Engineering questions

- How do we avoid losing a job after the API returns `202 Accepted` but before a broker sees the message?
- If a worker crashes during execution, how is a job recovered without reporting an incorrect result?
- How can duplicate HTTP requests and at-least-once message delivery be handled safely?
- What limits throughput: worker CPU, broker backlog, database contention, or request overhead?
- Can queue-depth-driven autoscaling improve job latency without sacrificing correctness?

## Architecture

```text
                 Client / load generator
                          |
                    REST / JSON
                          |
                 +--------v--------+
                 | FastAPI service |
                 | auth, validate  |
                 | idempotency key |
                 +--------+--------+
                          |
                 PostgreSQL transaction
                          |
          +---------------v----------------+
          | jobs / attempts / outbox_events|
          +---------------+----------------+
                          |
                    Outbox relay
                          |
                   RabbitMQ queue
                          |
                +---------+---------+
                |         |         |
           +----v---+ +---v----+ +--v-----+
           | Worker | | Worker | | Worker  |
           |   #1   | |   #2   | |   #N    |
           +----+---+ +---+----+ +--+-----+
                |         |         |
                +---------+---------+
                          |
              PostgreSQL state + results

      GitHub Actions: backend + frontend CI checks
      Kubernetes / optional EKS: future deployment
```

**Workload boundary:** The service executes a fixed allowlist of deterministic compute and bounded classical optimization tasks. It does **not** run arbitrary user-uploaded code, connect to a QPU, or claim to emulate a quantum computer.

## Planned stack

| Component | Choice | Why it is included |
| --- | --- | --- |
| Application | Python 3, FastAPI, Pydantic | Typed REST API, validation, autogenerated OpenAPI |
| Dashboard | React, TypeScript, Vite | Local job submission and inspection interface |
| Persistence | PostgreSQL, SQLAlchemy, Alembic | Transactional, durable job state and migrations |
| Job transport | RabbitMQ, Celery | Decoupled workers, acknowledgments, retry behavior |
| Quality | pytest, Ruff, mypy, HTTPX | Repeatable tests, linting, typing, API integration checks |
| Local environment | Docker Compose | One-command multi-service development |
| Cluster | Kubernetes (planned) | Workload isolation, probes, and later scaling experiments |
| Delivery | GitHub Actions | Pull-request and `main` branch quality checks |
| Operations | JSON application logs and `make doctor` | Lightweight local and container diagnostics |
| AWS environment | ECR, EKS, RDS (optional) | Cloud deployment through CloudForge |

## API contract

### Submit a job

`POST /v1/jobs` takes a bearer token and an `Idempotency-Key` scoped to the authenticated client. Reusing a key with the same request returns the original job; reusing it with different parameters returns `409 Conflict`. A new response reports the durably committed initial `ACCEPTED` state; the relay and worker advance it asynchronously afterward.

```http
POST /v1/jobs HTTP/1.1
Content-Type: application/json
Idempotency-Key: example-001

{
  "type": "monte_carlo_pi",
  "parameters": {"samples": 1000000, "seed": 42}
}
```

Example `202 Accepted` response:

```json
{
  "job_id": "01EXAMPLE",
  "state": "ACCEPTED",
  "status_url": "/v1/jobs/01EXAMPLE"
}
```

Additional endpoints:

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/jobs/{job_id}` | Return the client-visible state, timestamps, attempts, result/error |
| `GET /v1/jobs?limit=...&offset=...` | Paginated listing with per-client visibility |
| `POST /v1/jobs/{job_id}/cancel` | Planned: request cooperative cancellation where supported |
| `GET /healthz` | Liveness: application process is responsive |
| `GET /readyz` | PostgreSQL-backed readiness; returns `503` when unavailable |

Initial authentication can use a development-only API token; later use a production-appropriate identity provider. Tokens and DB credentials will never be embedded in container images or committed configuration.

### State machine

```text
ACCEPTED --> QUEUED --> RUNNING --> SUCCEEDED
                          |            |
                          |            +--> FAILED
                          +--> RETRY_WAIT --> QUEUED
                          +--> CANCELLED (when safe)
```

PostgreSQL is the source of truth. Each state transition must use validated state changes and concurrency-aware updates. `SUCCEEDED` requires a committed result; receiving a message does not imply job completion.

## Reliability and distributed-systems design

**Transactional outbox.** A single DB transaction inserts the job and its outbox event. The relay publishes to RabbitMQ with publisher confirms and marks the event delivered afterward. A relay crash can cause duplicate publications; the consumer must tolerate them. Outbox delivery should be monitored and replayable.

**At-least-once execution.** Acknowledge a task only after its durable state/result is committed. Redelivery is expected. Use unique job/attempt IDs, conditional claim updates, leases, and fencing tokens or equivalent generation checks to reject stale writes. A lease may allow overlapping *physical computations* during recovery: the design promises idempotent externally visible effects, **not** magically guaranteed exactly-once CPU execution.

**Retry discipline.** Distinguish retryable faults (connection loss, transient dependency failures) from permanent validation errors. Apply maximum attempts, exponential backoff with jitter, and a terminal failure state; make exhausted failures inspectable instead of retrying indefinitely.

**Backpressure.** Bound request size, exact-enumeration model size, per-client outstanding jobs, task timeouts, and worker concurrency. Use `429 Too Many Requests` when client quotas are added.

**Graceful shutdown.** Workers stop claiming new tasks, complete or safely relinquish in-flight work, and allow Kubernetes termination grace periods to expire predictably. Test termination under real load.

## Local development plan

First implement everything with Docker Compose before using Kubernetes.

```text
compose.yaml
  - api (FastAPI)
  - migrate (one-shot Alembic migration)
  - relay (outbox publisher)
  - worker (Celery)
  - postgres
  - rabbitmq
```

The current multi-service developer interface is:

```bash
make dev-up       # build and start dependencies and backend services
make dev-logs     # follow API, relay, worker, and RabbitMQ logs
make dev-down     # stop services
```

Keep local service dependencies replaceable via environment configuration and avoid cloud-only behavior in business logic.

## Kubernetes deployment roadmap

Kubernetes remains a later, deliberately separate milestone after local Compose
behavior and CI are stable. Initial cluster work will focus on API, relay, and
worker workloads; resource limits; health probes; configuration and secrets;
graceful worker termination; and repeatable rollback. Optional AWS deployment
will target a CloudForge-managed EKS environment. Helm, KEDA, GitOps tooling,
and autoscaling are not part of the current milestone.

## Automated testing and CI

`.github/workflows/ci.yml` runs for pull requests and pushes to `main` with
read-only repository permissions. The jobs are intentionally separate:

- **Backend:** Python 3.12 and uv install the locked project environment;
  `make backend-check` runs Ruff, strict mypy, and unit tests once. Dedicated
  targets then run PostgreSQL integration tests, lease fault-injection tests,
  and RabbitMQ end-to-end tests. GitHub service containers use an isolated
  `computegrid_test` database and disposable CI-only credentials.
- **Frontend:** Node.js 22 runs `npm ci`, TypeScript type checking, Vitest, and
  the Vite production build from the committed lockfile.

There is no deployment, registry publication, AWS access, or production secret
in this workflow. GitHub-hosted pull-request CI is the smallest fit because the
repository already lives on GitHub and needs only short-lived standard service
containers. Jenkins would add a controller, agents, upgrades, credentials,
plugins, and availability work without improving this project's current checks.
It can be reconsidered only if self-hosted or organization-wide pipeline needs
appear.

To reproduce the CI checks locally:

```bash
make dev-deps-up
make backend-check
make test-integration
make test-fault
make test-e2e
npm --prefix apps/web ci
make web-typecheck web-test web-build
```

Repository maintainers may optionally make the `Backend` and `Frontend` checks
required in the GitHub `main` branch protection rules. No GitHub secrets are
needed for CI.

**Failure-injection acceptance tests**

| Injected condition | Expected, testable outcome |
| --- | --- |
| Client retries the same submission | One logical job; identical response reference |
| API dies after DB commit | Outbox relay still eventually enqueues the job |
| Relay dies after publishing | Duplicate message is safe; one committed result |
| Worker dies during execution | Retry/recovery; no incorrect terminal success |
| Broker briefly disconnects | Backoff and recovery, no silent lost jobs |
| Database unavailable | API fails safely; readiness reflects inability to serve |
| Queue grows under load | Backlog remains durable and saturation is measurable |
| Worker scaled down | In-flight task handled according to termination policy |

## Structured logging and lightweight operations

API, relay, and worker application events are emitted as one JSON object per
line to stdout for Docker-compatible collection. Every record has an ISO-8601
UTC timestamp, severity, service, event, and message. Lifecycle events add
`job_id`, `attempt_number`, and `worker_id` when those values exist. Submission,
confirmed queueing, worker claim, success, durable retry, lease recovery, and
terminal failure are covered. Exceptions retain traceback context, while job
parameters, API tokens, broker URLs, and database credentials are not logged.

At this scale, Docker logs, health/readiness probes, RabbitMQ's built-in queue
counters, and `make doctor` answer the immediate operational questions. A
dedicated metrics, tracing, or log-storage platform would add services and
maintenance before there is a measured retention, alerting, or correlation
need. M5 will first gather real queue and worker measurements; that evidence can
justify later observability work if the lightweight tools become insufficient.

## Performance direction

Measure **job submissions/sec, completed jobs/sec, queue wait p50/p95, execution p50/p95, HTTP p95, retry/failure rate, backlog, CPU/memory**, and DB connection utilization. Every benchmark records workload seed, request count, payload size, worker replicas, resource limits, and tool versions. Use fixed workload classes; do not compare unlike task types.

Benchmark results should live in `docs/benchmarks/` with exact reproduction commands; no fabricated speedup claims.

## Implementation milestones

- [x] **M1 — Python service:** FastAPI, request validation, SQLAlchemy models, Alembic, pytest, Docker Compose.
- [x] **M2 — Reliable execution:** RabbitMQ/Celery workers, transactional outbox, idempotency, retry, recovery and failure-injection tests.
- [x] **M2.5 — Optimization and UI:** bounded exact Weighted Max-Cut/QUBO workload and polished operations dashboard.
- [x] **M2.6 — Generic models:** exact BINARY/SPIN BQM jobs, restricted binary/linear CQM jobs, and educational model-selection UI.
- [x] **M3 — CI and lightweight operations:** GitHub Actions, structured logging, readiness checks, and read-only diagnostics.
- [ ] **M4 — Kubernetes fundamentals:** workload manifests, probes, resource controls, graceful termination, local cluster tests, and optional AWS EKS practice.
- [ ] **M5 — Public operations demo:** controlled load generation, live queue/worker visualization, crash-recovery demonstration, and basic performance measurements.

**MVP definition of done:** Jobs can be submitted, retrieved, and executed locally; all specified duplicate-submission and worker-restart tests pass; the CI pipeline verifies the result. EKS is *not* required to call the backend MVP complete.

## Proposed repository layout

```text
computegrid/
├── README.md
├── apps/
│   ├── api/
│   ├── worker/
│   └── outbox_relay/
├── packages/common/
├── migrations/
├── tests/{unit,integration,fault,load}/
├── deploy/kubernetes/              # future milestone
├── docs/{architecture,benchmarks,runbooks}/
├── scripts/
├── compose.yaml
├── Makefile
└── .github/workflows/
```

## Safety, limits, and cost

Do not accept arbitrary Python/scripts for execution, expose brokers/databases publicly, or commit credentials. Default to Docker Compose and `kind`; AWS deployment is optional and must use CloudForge's budget checks and automated teardown. Time and throughput targets are measurements to establish, not claims already met.

## Skills demonstrated

Python application development, REST API design, mathematical optimization, SQL/database transactions, Linux containers, Git/GitHub Actions, fault-tolerant distributed systems, Kubernetes, and automated tests.

## Connection to the portfolio

- ****CloudForge**:** Can provide an optional EKS environment in a later deployment milestone.

## License

License to be selected before publication.
