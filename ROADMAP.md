# ComputeGrid — Engineering Roadmap

Development history, architecture decisions, technical challenges, and validation results.

For the project overview, demo, and setup instructions, see [README.md](README.md).

## 1. Development Timeline

ComputeGrid was developed incrementally, starting as a job submission API and evolving into a fault-tolerant distributed execution platform.

| Phase | Milestone | Outcome |
|---|---|---|
| M1 | API & Persistence | FastAPI, PostgreSQL, idempotent job submission, React dashboard |
| M2 | Distributed Execution | RabbitMQ, Celery, transactional outbox, retries, crash recovery |
| M3 | CI & Observability | GitHub Actions, automated tests, structured logging, health checks |
| M4 | Kubernetes | Local deployment, horizontal scaling, self-healing, failure recovery |
| M5 | Interactive Demo & Performance | Live job visualization, computational load demo, scaling benchmarks |

All five phases were implemented and validated within the local development scope. Public hosting was deliberately excluded from the final project scope.

---

## 2. Architecture Evolution

### M1 — Building the Job Processing API

**Problem:** Computational jobs may take seconds or minutes to complete. Keeping HTTP requests open until execution finishes couples API responsiveness to execution time.

**Decision:** Separate job submission from execution.

- FastAPI accepts and validates requests.
- PostgreSQL persists job state and execution history.
- Clients receive a Job ID and retrieve results asynchronously.
- Idempotency keys prevent duplicate job creation on repeated submissions.

**Outcome:** A durable API and dashboard that support asynchronous job submission and result retrieval.

### M2 — Introducing Distributed Workers

**Problem:** Job execution should scale independently from the API while surviving message redelivery and worker failures.

**Decision:** Use RabbitMQ for message delivery, Celery for background execution, and PostgreSQL as the authoritative state store.

```text
API -> PostgreSQL -> Outbox Relay -> RabbitMQ
                                          |
                                      Celery Workers
                                          |
                                      PostgreSQL
```

Key reliability mechanisms:

- **Transactional Outbox:** Atomically records jobs and publication events.
- **Publisher Confirms:** Confirms broker acceptance before recording publication progress.
- **Idempotent Execution:** Guards against duplicate or stale task deliveries.
- **Bounded Retries:** Uses exponential backoff and maximum attempt limits.
- **Worker Leases:** Detects abandoned execution attempts.
- **Fencing:** Prevents outdated attempts from overwriting authoritative results.

**Outcome:** An at-least-once execution system with durable state transitions and worker crash recovery.

### M3 — Building Confidence Through Automation

**Problem:** Correctness in a distributed system depends on failure handling and concurrency, not just successful API requests.

**Decision:** Introduce automated verification and operational diagnostics.

- Unit, integration, end-to-end, and fault-injection tests.
- GitHub Actions with Ruff, mypy, pytest, TypeScript, and Vitest.
- Structured JSON logging correlated by Job ID.
- API liveness and readiness endpoints.
- Read-only operational diagnostics.

**Outcome:** Reproducible verification of application behavior, dependency failures, and critical reliability properties.

### M4 — Moving to Kubernetes

**Problem:** Validate that the platform can operate beyond a single manually managed Compose configuration.

**Decision:** Deploy to a local single-node Kubernetes cluster using kind.

- Kubernetes Deployments for API, relay, and workers.
- StatefulSets and persistent volumes for PostgreSQL and RabbitMQ.
- Services for internal discovery.
- ConfigMaps and Secrets for configuration.
- Horizontal worker scaling from one to four replicas.
- Pod replacement, rolling restarts, and in-flight failure recovery.

**Outcome:** Verified Kubernetes orchestration, task distribution across replicas, and application-level recovery after a worker crash.

All Pods shared one physical host. This experiment verified orchestration behavior, not multi-machine high availability.

### M5 — Building an Observable Computational Demo

**Problem:** Backend logs alone make it difficult to demonstrate what happens inside a distributed queue-based system.

**Decision:** Build a React operations console backed by actual PostgreSQL, RabbitMQ, and Celery state.

- Real-time Queue → Worker → Completion visualization.
- Per-worker job assignment and execution history.
- Anonymous demo sessions with isolation and rate limits.
- Server-controlled computational workload generation.
- Four CPU-based workloads: Monte Carlo π, Weighted Max-Cut, BQM, and CQM.
- Reproducible performance benchmarks across worker replicas.

**Outcome:** An interactive local demo showing real distributed execution, supported by measured throughput and latency results.

---

## 3. Engineering Challenges & Problem Solving

The following cases document issues encountered during implementation and the reasoning behind their solutions.

### Case 1 — Avoiding the Database–Message Broker Dual-Write Problem

**Problem**

A naive implementation would persist a Job in PostgreSQL and then publish a message to RabbitMQ.

If the API crashes between those operations, the database contains an accepted Job that may never execute.

**Solution**

Implemented the Transactional Outbox Pattern.

1. Insert the Job and its outbox event in one PostgreSQL transaction.
2. Let the outbox relay select pending events using `FOR UPDATE SKIP LOCKED`.
3. Publish each event to RabbitMQ with publisher confirmation.
4. Record publication progress after confirmation.
5. Retry unpublished events following transient failures.

**Trade-off**

A relay can crash after successful publication but before recording it. Therefore, duplicate messages remain possible.

Instead of claiming exactly-once execution, workers use guarded state transitions and attempt fencing to tolerate repeated delivery.

**Lesson:** Reliable distributed messaging requires explicitly handling the failure window between independent systems.

### Case 2 — Recovering a Job After a Worker Crash

**Problem**

A worker may terminate after claiming a Job but before persisting its result.

Restarting the worker process alone does not guarantee recovery of the unfinished application-level execution.

**Solution**

Implemented persisted execution attempts with fixed-duration leases and fencing.

```text
RUNNING
   |
   | Worker crashes
   v
Lease expires
   |
   v
Attempt ABANDONED
   |
   v
Retry scheduled
   |
   v
New worker claims Job
   |
   v
SUCCEEDED
```

**Verification**

An actual in-flight worker was terminated with `SIGKILL` in the local Kubernetes environment.

- Kubernetes restarted the failed container.
- The original attempt became `ABANDONED`.
- A second attempt completed on another worker.
- End-to-end recovery took approximately **33.58 seconds** with a temporary 30-second lease.

The normal lease configuration is 300 seconds.

**Lesson:** Container self-healing and application-level recovery solve different problems. Both are necessary, and neither provides exactly-once physical execution.

### Case 3 — Isolating End-to-End Tests from Running Workers

**Problem**

An end-to-end test worker and the existing Docker Compose workers could consume messages from the same RabbitMQ queue.

That made test execution dependent on whichever consumer received a task.

**Solution**

Assigned a dedicated queue to the E2E test environment so its worker would not compete with ordinary application workers.

The test environment could then validate the intended execution path without accidentally handing jobs to an unrelated worker.

**Lesson:** Integration and end-to-end tests need isolation at the messaging layer, not just separate database records.

### Case 4 — Fixing Delayed Job Flow Visualization

**Problem**

The initial operations console refreshed too slowly to accurately show short-lived job transitions.

A Job could enter and leave the queue between refreshes, making the visualization appear unresponsive.

**Investigation**

The original operations snapshot combined several different data sources:

- PostgreSQL job state.
- RabbitMQ queue metrics.
- Celery worker discovery and activity.

These sources had different response times and polling requirements.

**Solution**

Separated operational polling into two paths:

| Data | Update interval |
|---|---|
| Visitor job state and persisted worker assignments | ~500ms |
| RabbitMQ metrics, worker discovery, aggregate counts | ~3s |

The fast path performs bounded, session-scoped PostgreSQL reads without inspecting RabbitMQ or Celery.

Requests are serialized, obsolete responses are ignored, and polling stops cleanly when the relevant UI is unmounted.

**Result**

Job blocks respond more quickly to persisted state changes without increasing expensive broker and worker inspection frequency.

**Lesson:** Observability systems should separate high-frequency application state from slower infrastructure metrics.

### Case 5 — Diagnosing Unexpected Worker Concurrency

**Problem**

During a laptop demonstration, several Jobs appeared simultaneously inside a single Worker card, despite the intended configuration of four independent workers with one execution process each.

Initially, the behavior appeared to be related to slower laptop hardware or a UI issue.

**Investigation**

Celery's `inspect active` command showed multiple active tasks with different process IDs inside individual worker containers.

Inspecting the resolved Docker Compose configuration revealed:

```text
Worker containers: 4
COMPUTEGRID_WORKER_CONCURRENCY=4

Maximum concurrent execution slots: 16
```

The Compose default had been changed to concurrency 1, but an existing `.env` value overrode it.

**Solution**

Explicitly set:

```dotenv
COMPUTEGRID_WORKER_CONCURRENCY=1
```

Recreated the worker containers and verified the resolved configuration.

**Lesson:** Container replica count and worker process concurrency are independent dimensions. Configuration overrides must be checked against actual runtime behavior.

### Case 6 — Resolving Anonymous Demo Session Failures

**Problem**

Jobs submitted through the anonymous demo could return a successful creation response, while subsequent requests for the same Job returned `404`.

**Investigation**

The browser was using inconsistent local hostnames. Because anonymous ownership relies on an HttpOnly session cookie, switching between `localhost` and `127.0.0.1` changed which cookies were sent.

**Solution**

Used consistent hostnames for the frontend and API during local development.

**Lesson:** Successful HTTP responses do not necessarily establish session continuity. Cookie scope and request origin matter when diagnosing apparently inconsistent API behavior.

---

## 4. Performance Engineering

### Experimental Design

To evaluate horizontal scaling, identical CPU-intensive workloads were executed with one, two, and four independent Celery workers.

Each configuration used:

- 100 Monte Carlo π jobs per trial.
- 5,000,000 samples per Job.
- Celery concurrency of one per worker.
- Submission concurrency of four.
- Three repetitions per configuration.
- The same desktop environment and application stack.

### Results

| Workers | Mean Throughput | Speedup | Efficiency |
|---|---:|---:|---:|
| 1 | 1.068 jobs/s | 1.00× | 100% |
| 2 | 2.096 jobs/s | 1.96× | 98.1% |
| 4 | 3.879 jobs/s | 3.63× | 90.8% |

Additional results:

| Metric | 1 Worker | 4 Workers |
|---|---:|---:|
| Mean batch duration | 93.61s | 25.78s |
| Mean p50 queue latency | 46.33s | 12.17s |
| Mean p50 execution latency | 0.928s | 0.970s |
| Mean p95 end-to-end latency | 88.57s | 24.23s |

All **900 submitted jobs completed successfully** with zero submission failures.

### Interpretation

The system achieved **3.63× throughput scaling at 90.8% efficiency** when increasing from one to four workers.

The improvement was close to linear, although scaling efficiency decreased modestly at four workers. Potential contributors include shared CPU resources, database operations, messaging overhead, and scheduling contention; this experiment does not independently establish which factor dominates.

Throughput was computed from server-side job timestamps. Queue, execution, and end-to-end latency percentiles were calculated from persisted timestamps.

The reported latency values above are means of per-trial percentiles, not pooled percentiles over all jobs.

### Lessons from Benchmarking

Earlier comparisons between runs performed under different conditions suggested superlinear scaling. Those numbers were not used as the final scaling baseline.

A controlled nine-run sweep produced a reproducible comparison where worker count was the intended independent variable.

This reinforced the importance of controlled environments, repeated measurements, configuration verification, and clear metric definitions.

### Reproducibility

The benchmark harness is available in:

- `scripts/benchmark_jobs.py`
- `scripts/benchmark_cpu_scaling.py`
- `benchmark-results/` (recorded result files, when included in the repository)

The primary scaling experiment was run using Docker Compose and does not represent distributed execution across four physical machines.

---

## 5. Validation Summary

| Area | Validation |
|---|---|
| API & Persistence | Unit and PostgreSQL integration tests |
| Distributed Execution | RabbitMQ/Celery end-to-end tests |
| Retries & Fencing | Automated fault-injection tests |
| CI | GitHub Actions backend and frontend checks |
| Kubernetes | Deployment, scaling, rolling restart, self-healing |
| Crash Recovery | Verified in-flight `SIGKILL` recovery |
| Performance | Nine controlled trials, 900 successful jobs |
| Interactive Demo | Real queue, worker assignments, and completion visualization |

## 6. Scope Decisions and Limitations

ComputeGrid was built as an engineering portfolio project rather than a production cloud service.

**Deliberately excluded from the final scope:**

- Public cloud hosting and managed production infrastructure.
- Browser-triggered worker termination controls.
- Production authentication and multi-tenant identity management.
- Prometheus, Grafana, OpenTelemetry, and a dedicated monitoring stack.
- Multi-node high availability, database replication, and disaster recovery.
- Arbitrary user-submitted executable workloads.
- Quantum hardware execution or integration with an external quantum solver.

These choices kept development focused on the core goals: distributed execution, reliability, observability, and measured performance.

**Reliability boundary:** At-least-once delivery, bounded retries, and fencing protect durable job state, but physical execution can occur more than once. External side effects would need their own idempotency safeguards.

**Deployment boundary:** The platform was validated with Docker Compose and local Kubernetes. It was not operated as a publicly hosted production service.

---

## 7. Final Outcome

ComputeGrid evolved from a single job submission API into a locally deployable distributed computation platform with transactional messaging, independent workers, persistent execution history, crash recovery, automated verification, and live operational visualization.

The final performance evaluation demonstrated near-linear scaling across four Celery workers.

Development concluded with the core engineering objectives validated, while keeping public hosting and further infrastructure expansion outside the project scope.