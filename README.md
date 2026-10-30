
# ComputeGrid

**A fault-tolerant distributed job-processing platform built with Python, FastAPI, PostgreSQL, RabbitMQ, Celery, and React.**

> **Status (October 2026):** M1–M3 are implemented. Backend and frontend CI pass on GitHub Actions. Distributed execution, retry handling, lease-based crash recovery, structured logging, readiness checks, and operational diagnostics have been verified.
>
> **Next:** M4 — Kubernetes deployment and horizontal scaling. M5 — Interactive queue/worker demo, controlled load generation, failure recovery visualization, and public demonstration.
>
> **Project context:** ComputeGrid is an independent portfolio project inspired by the reliability and infrastructure challenges of cloud compute services. It is not a D-Wave product, an implementation of D-Wave's Hybrid Solver Service, or affiliated with D-Wave. All current workloads execute on classical CPUs; no QPU or external quantum solver is used.

## 1. Purpose

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
- Provides development-only bearer-token authentication.
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

The implementation uses fencing checks to prevent an outdated attempt from committing a result after recovery.

**Verified failure experiment:** An in-flight Celery worker was terminated with `SIGKILL`. The original attempt was recorded as `ABANDONED` with `WorkerLeaseExpired`, and a subsequent attempt completed successfully.

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
| Developer tooling | Make, Bash | Testing and diagnostics |

## 7. Running Locally

### Prerequisites

- Docker Engine with Docker Compose.
- Python 3.12 and the project's Python dependency toolchain.
- Node.js and npm.
- Git.

No AWS credentials, D-Wave Leap access, or quantum hardware are required.

### Environment Configuration

From the repository root:

```bash
cp .env.example .env
```

Edit `.env` to configure local database and broker credentials, connection settings, development API token, and CORS origins.

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

Enter the configured development bearer token in the dashboard.

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

Deployment automation will be considered after defining the Kubernetes and hosting environments in M4.

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

Formal 1/2/4-worker throughput measurements are planned for M5.

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
| M4 | Kubernetes deployment and scaling | Planned |
| M5 | Interactive demo, load experiments, failure visualization, public deployment | Planned |

### M4 — Kubernetes Fundamentals

Deploy ComputeGrid on a local Kubernetes cluster using plain YAML manifests.

Planned work:

- Run a local cluster using kind, K3s, or an equivalent environment.
- Define Deployments and Services.
- Configure ConfigMaps and Secrets.
- Configure container probes and termination behavior.
- Deploy the API, relay, and worker components.
- Integrate PostgreSQL and RabbitMQ using an appropriately scoped local deployment strategy.
- Practice `kubectl get`, `describe`, `logs`, and `scale`.
- Scale Celery workers from 1 to 2 to 4 replicas.
- Verify concurrent execution and queue consumption.
- Delete worker Pods and observe replacement behavior.
- Verify that Kubernetes Pod replacement and application-level lease recovery are distinct mechanisms.
- Practice rolling updates and basic troubleshooting.

Use ordinary manifests before considering Helm.

**Optional AWS EKS exercise**

A short-lived EKS deployment may be used to explore:

- Amazon EKS cluster operation.
- IAM and ECR integration.
- Kubernetes deployment on AWS.
- Cloud networking and service exposure.

Estimate costs before provisioning and remove billable infrastructure afterward.

EKS is not required to operate the project's eventual public demo. A lower-cost hosting environment may be more suitable for persistent availability.

### M5 — Interactive Demo and Reliability Evidence

Build a real-data-driven demonstration of ComputeGrid's queue-based execution and failure recovery.

#### Queue and Worker Demo Console

Extend the existing React dashboard without removing the New Job panel.

Place a compact operational visualization above Recent Jobs.

Show:

- RabbitMQ queue depth.
- Ready and unacknowledged messages.
- Worker availability and current assignments.
- Job movement from queued to running to completion.
- Attempt history and recovery events.

Use four worker cards with a subtle rabbit-inspired visual design.

The interface should remain professional and consistent with the existing dashboard.

**All displayed operational state must be derived from actual backend data, not a frontend-only simulation.**

#### Controlled Load Generator

Provide a demo control that submits jobs for a bounded 20-second period.

Support three submission rates:

- Low
- Medium
- High

The rates should be explicitly defined, bounded, and documented.

Demonstrate how queue depth and waiting time change under different arrival rates.

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

#### Performance Experiments

Compare worker configurations:

- 1 worker.
- 2 workers.
- 4 workers.

Measure:

- Completed jobs per second.
- Job waiting time.
- End-to-end completion latency.
- Queue-depth behavior.
- Failure recovery timing.

Use fixed workload settings and record the test environment.

Do not invent benchmark results or assume linear scaling.

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

- Development bearer-token authentication is not production-grade identity management.
- API idempotency and worker message deduplication solve different problems.
- At-least-once delivery does not imply exactly-once physical execution.
- Fixed execution leases require bounded workloads.
- External side effects need additional idempotency safeguards.
- PostgreSQL and RabbitMQ must not be exposed directly to the public internet.
- Credentials must never be committed to Git.
- Public deployment requires appropriate authentication, abuse protection, and resource controls.
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

The next engineering goals are Kubernetes-based orchestration and a live demonstration of horizontal scaling, queue behavior, and fault recovery.

The central objective is to demonstrate **distributed-systems correctness, automated validation, and practical cloud operations through a small, explainable system**.
