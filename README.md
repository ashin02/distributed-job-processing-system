# ComputeGrid / Fault-Tolerant Distributed Job Processing System

**A fault-tolerant Python job-processing service: REST API, durable queues, distributed workers, and Kubernetes autoscaling.**

> **Status:** Milestone 2.5 is implemented for local development. Operability, Kubernetes, cloud deployment, and measured performance work remain on the roadmap.
>
> **Context:** Inspired by general engineering challenges in cloud-hosted compute services. This is an independent learning project, not a D-Wave product, implementation, or affiliation.

## Current implementation: Milestone 2.5

The repository contains the FastAPI application, PostgreSQL persistence, authenticated job APIs, a transactional outbox relay, RabbitMQ transport, Celery workers, and a React/TypeScript operations dashboard. New jobs and their outbox events are committed atomically, client-scoped idempotency is enforced by PostgreSQL, and both deterministic `monte_carlo_pi` and bounded `weighted_max_cut` jobs execute asynchronously. Weighted Max-Cut graphs are converted to QUBO and solved by exact classical enumeration; this is an optimization demonstration, not a quantum computer emulator. The relay uses RabbitMQ publisher confirms before marking events published, while workers acknowledge late only after a terminal result, durable retry, or permanent failure is committed. Retryable execution failures use persisted retry limits and bounded exponential backoff with jitter. Every running attempt has a durable lease; the relay recovers expired attempts with a new fencing generation so late workers cannot overwrite recovered results. Autoscaling, caching, monitoring, and Kubernetes deployment remain deferred.

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

Open `http://127.0.0.1:5173`, paste the development API token from `.env`, and connect. The token is retained only in browser session storage. The dashboard supports both job types, manual refresh, optional five-second history polling, and paginated job history. Weighted Max-Cut results include an SVG graph, highlighted optimal partition, QUBO coefficients, and solver metadata. A selected active job refreshes automatically every second, and its execution timeline retains persisted lifecycle timestamps, durations, retries, abandoned attempts, results, and errors after completion. Set `VITE_API_BASE_URL` in `apps/web/.env` when the API is not available at `http://localhost:8000`.

FastAPI allows the local Vite origins configured by `COMPUTEGRID_CORS_ORIGINS`. The value is a JSON array; keep production origins out of this development configuration.

The API is available at `http://127.0.0.1:8000`. Its liveness endpoint can be checked with:

```bash
curl --fail http://127.0.0.1:8000/healthz
```

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

Build a small cloud computing service that can **accept, track, execute, retry, and observe compute jobs** while surviving worker failures. The primary objective is to demonstrate practical Python backend engineering, Linux-based operations, testing, and distributed-systems correctness. An accompanying repository, **CloudForge**, will provision an optional AWS EKS environment; **OpsLens** will supply end-to-end observability.

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

      Redis: bounded API cache and request throttling
      KEDA: RabbitMQ queue length -> worker scaling
      OpsLens: metrics, logs, traces, alerts
      CloudForge: CI/CD, ECR, EKS, infrastructure
```

**Workload boundary:** The service executes a fixed allowlist of deterministic compute and bounded classical optimization tasks. It does **not** run arbitrary user-uploaded code, connect to a QPU, or claim to emulate a quantum computer.

## Planned stack

| Component | Choice | Why it is included |
| --- | --- | --- |
| Application | Python 3, FastAPI, Pydantic | Typed REST API, validation, autogenerated OpenAPI |
| Dashboard | React, TypeScript, Vite | Local job submission and inspection interface |
| Persistence | PostgreSQL, SQLAlchemy, Alembic | Transactional, durable job state and migrations |
| Job transport | RabbitMQ, Celery | Decoupled workers, acknowledgments, retry behavior |
| Cache / throttling | Redis | TTL-based read caching; bounded client request limits |
| Quality | pytest, Ruff, mypy, HTTPX | Repeatable tests, linting, typing, API integration checks |
| Local environment | Docker Compose | One-command multi-service development |
| Cluster | Kubernetes, Helm, KEDA | Workload isolation, probes, autoscaling |
| Delivery | GitHub Actions, Argo CD | CI, immutable image promotion, GitOps deployment |
| Observability | OpenTelemetry, Prometheus | Request/job telemetry exposed to OpsLens |
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

| Endpoint | Planned behavior |
| --- | --- |
| `GET /v1/jobs/{job_id}` | Return the client-visible state, timestamps, attempts, result/error |
| `GET /v1/jobs?limit=...&offset=...` | Paginated listing with per-client visibility |
| `POST /v1/jobs/{job_id}/cancel` | Planned: request cooperative cancellation where supported |
| `GET /healthz` | Liveness: application process is responsive |
| `GET /readyz` | Planned: readiness to safely serve new requests |
| `GET /metrics` | Planned: Prometheus metrics with bounded label cardinality |

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

**Backpressure.** Bound request size, per-client outstanding jobs, task timeouts, and worker concurrency. Use `429 Too Many Requests` when client quotas are exceeded. Redis caching must never become the authoritative source of job state; cache invalidation and TTL must be explicit.

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

Redis remains deferred. The current multi-service developer interface is:

```bash
make dev-up       # build and start dependencies and backend services
make dev-logs     # follow API, relay, worker, and RabbitMQ logs
make dev-down     # stop services
```

Keep local service dependencies replaceable via environment configuration and avoid cloud-only behavior in business logic.

## Kubernetes deployment and scaling

The ComputeGrid Helm chart will own the API, outbox relay, and worker Deployments, Services, probes, and runtime configuration. The **CloudForge** repository will manage the cluster, shared addons, and GitOps deployment entry points. Argo CD, rather than CI using `kubectl apply`, will reconcile production-like application state.

Planned cluster controls:

- Separate worker and API resource requests/limits; non-root containers and read-only root filesystems where feasible.
- Proper startup/readiness/liveness probes; graceful termination for jobs.
- KEDA RabbitMQ trigger for queue-length-driven worker scaling, configured from load-test measurements.
- Kubernetes Secrets sourced from AWS Secrets Manager in the AWS phase; no plaintext secrets in Git.
- NetworkPolicy restricting traffic to the services that actually need it (subject to the installed CNI's enforcement support).
- Immutable image digests promoted by GitOps pull requests, with documented rollback procedures.

Example **conceptual** KEDA resource (requires a real RabbitMQ authentication resource and chart wiring):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: computegrid-workers
spec:
  scaleTargetRef:
    name: computegrid-worker
  minReplicaCount: 1
  maxReplicaCount: 8
  triggers:
    - type: rabbitmq
      metadata:
        queueName: computegrid.jobs
        mode: QueueLength
        value: "20"
      authenticationRef:
        name: computegrid-rabbitmq-auth
```

## Automated testing and CI

GitHub Actions will run on every pull request:

1. Format/lint/type checks and dependency vulnerability checks.
2. `pytest` unit tests for API schemas, state transitions, retries, and cache invalidation.
3. Integration tests against ephemeral PostgreSQL, RabbitMQ, and Redis services.
4. Contract tests verifying API status codes and idempotent submission.
5. Container build and image vulnerability scan.
6. On approved merge: publish an immutable image to the registry; use a separate GitOps PR to request deployment.

**Failure-injection acceptance tests**

| Injected condition | Expected, testable outcome |
| --- | --- |
| Client retries the same submission | One logical job; identical response reference |
| API dies after DB commit | Outbox relay still eventually enqueues the job |
| Relay dies after publishing | Duplicate message is safe; one committed result |
| Worker dies during execution | Retry/recovery; no incorrect terminal success |
| Broker briefly disconnects | Backoff and recovery, no silent lost jobs |
| Database unavailable | API fails safely; readiness reflects inability to serve |
| Queue grows under load | KEDA adds workers within configured replica limits |
| Worker scaled down | In-flight task handled according to termination policy |

## Performance and observability

Measure **job submissions/sec, completed jobs/sec, queue wait p50/p95, execution p50/p95, HTTP p95, retry/failure rate, backlog, CPU/memory**, and DB connection utilization. Every benchmark records workload seed, request count, payload size, worker replicas, resource limits, and tool versions. Use fixed workload classes; do not compare unlike task types.

Instrument traces for `API -> DB/outbox -> broker -> worker -> DB commit` with stable correlation IDs propagated in message headers. Export metrics, logs, and traces to OpsLens. Avoid high-cardinality labels containing job IDs. Results should live in `docs/benchmarks/` with exact reproduction commands; no fabricated speedup claims.

## Implementation milestones

- [x] **M1 — Python service:** FastAPI, request validation, SQLAlchemy models, Alembic, pytest, Docker Compose.
- [x] **M2 — Reliable execution:** RabbitMQ/Celery workers, transactional outbox, idempotency, retry, recovery and failure-injection tests.
- [x] **M2.5 — Optimization and UI:** bounded exact Weighted Max-Cut/QUBO workload and polished operations dashboard.
- [ ] **M3 — Operability:** Structured logging, OpenTelemetry, metrics, Redis cache/quota, operational runbooks.
- [ ] **M4 — Kubernetes:** Helm chart, probes, KEDA, local `kind` tests, GitHub Actions and GitOps release.
- [ ] **M5 — AWS deployment:** Run through CloudForge-managed EKS and supporting services; benchmark and document costs.

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
├── deploy/helm/computegrid/
├── dashboards/
├── docs/{architecture,benchmarks,runbooks}/
├── scripts/
├── compose.yaml
├── Makefile
└── .github/workflows/
```

## Safety, limits, and cost

Do not accept arbitrary Python/scripts for execution, expose brokers/databases publicly, or commit credentials. Default to Docker Compose and `kind`; AWS deployment is optional and must use CloudForge's budget checks and automated teardown. Time and throughput targets are measurements to establish, not claims already met.

## Skills demonstrated

Python application development, REST API design, SQL/database transactions, caching, Linux containers, Git/GitHub Actions, fault-tolerant distributed systems, Kubernetes/KEDA, and automated tests.

## Connection to the portfolio

- ****OpsLens**:** Instruments ComputeGrid, detects incidents, and generates diagnostic bundles.
- ****CloudForge**:** Builds EKS, ECR, networking, IAM, GitOps, and the deployment workflows for both services.

## License

License to be selected before publication.
