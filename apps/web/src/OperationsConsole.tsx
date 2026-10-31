import { CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { poll } from "./poll";
import { ComputeDemo } from "./ComputeDemo";

import {
  DependencyStatus,
  getOperationsSummary,
  getJobFlow,
  JobState,
  OperationsJob,
  OperationsSummary,
  OperationsWorker,
} from "./api";

const REFRESH_INTERVAL_MS = 3_000;
const COMPLETION_DISPLAY_MS = 1_600;
const MAX_VISIBLE_JOBS = 6;
const JOB_STATE_ORDER: JobState[] = [
  "ACCEPTED", "QUEUED", "RUNNING", "RETRY_WAIT", "SUCCEEDED", "FAILED", "CANCELLED",
];

interface OperationsConsoleProps {
  enabled: boolean;
  token: string | null;
  onJobCompleted?: (jobId: string) => void;
  onJobsChanged?: () => void;
}

function dependencyLabel(status: DependencyStatus): string {
  return status === "AVAILABLE" ? "ONLINE" : status;
}

function workerState(worker: OperationsWorker): "ONLINE" | "IDLE" | "BUSY" | "UNKNOWN" {
  if (worker.availability !== "AVAILABLE") return "UNKNOWN";
  if (worker.activity === "IDLE" || worker.activity === "BUSY") return worker.activity;
  return "ONLINE";
}

function workerIdentity(worker: OperationsWorker): string | null {
  return worker.worker_id ?? worker.worker_key ?? null;
}

function workerLabel(worker: OperationsWorker, index: number): string {
  return worker.label ?? worker.worker_id ?? `Worker ${index + 1}`;
}

function transitionName(prefix: string, value: string): string {
  return `${prefix}-${value.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function workloadLabel(type: OperationsJob["job_type"]): string {
  return {
    monte_carlo_pi: "MC",
    weighted_max_cut: "MAX-CUT",
    bqm: "BQM",
    cqm: "CQM",
  }[type];
}

function JobBlock({ job, note }: { job: OperationsJob; note?: string }) {
  const blockState = job.state === "RETRY_WAIT" && job.attempt_state === "ABANDONED"
    ? "ABANDONED" : job.state;
  return (
    <div
      className={`flow-job flow-job-${blockState.toLowerCase()}`}
      data-job-id={job.job_id}
      data-job-state={blockState}
      style={{ viewTransitionName: transitionName("job", job.job_id) } as CSSProperties}
      title={`${workloadLabel(job.job_type)} · ${job.job_id} · ${note ?? blockState}`}
    >
      <strong>{workloadLabel(job.job_type)}</strong><span>#{job.job_id.slice(0, 4)}</span>
      {note && <small>{note}</small>}
    </div>
  );
}

function QueueLane({ jobs, ready, unacknowledged, visibleStatus }: {
  jobs: OperationsJob[];
  ready: number | null;
  unacknowledged: number | null;
  visibleStatus: DependencyStatus;
}) {
  const visible = jobs.slice(0, MAX_VISIBLE_JOBS);
  const overflow = jobs.length - visible.length;
  return (
    <section className="queue-lane" aria-label="Queue lane">
      <div className="flow-section-heading">
        <div><strong>Queue</strong><span>Your confirmed waiting jobs</span></div>
        <div className="queue-counters">
          <span>Ready <b>{ready ?? "Unavailable"}</b></span>
          <span>Unacked <b>{unacknowledged ?? "Unavailable"}</b></span>
        </div>
      </div>
      <div className="queue-track">
        {visible.length === 0
          ? <span className="flow-placeholder">
            {visibleStatus === "AVAILABLE"
              ? "No visitor jobs confirmed in queue"
              : "Visitor job data unavailable"}
          </span>
          : visible.map((job) => <JobBlock job={job} key={job.job_id} />)}
        {overflow > 0 && <span className="flow-overflow">+{overflow} more</span>}
      </div>
    </section>
  );
}

function WorkerCard({ worker, index, jobs }: {
  worker: OperationsWorker;
  index: number;
  jobs: OperationsJob[];
}) {
  const state = workerState(worker);
  return (
    <article
      className={`flow-worker flow-worker-${state.toLowerCase()}`}
      style={{
        viewTransitionName: transitionName("worker", workerIdentity(worker) ?? String(index)),
      } as CSSProperties}
    >
      <div className="flow-worker-heading">
        <strong>{workerLabel(worker, index)}</strong>
        <span className={`worker-state worker-state-${state.toLowerCase()}`}>{state}</span>
      </div>
      <div className="flow-worker-slot">
        {jobs.length > 0 ? <>
          {jobs.slice(0, 2).map((job) => <JobBlock job={job} note="RUNNING" key={job.job_id} />)}
          {jobs.length > 2 && <span className="flow-overflow">+{jobs.length - 2} more</span>}
        </>
          : worker.current_job_id ? <div
            className="flow-job flow-job-running"
            style={{
              viewTransitionName: transitionName("job", worker.current_job_id),
            } as CSSProperties}
            title={`Running Job ${worker.current_job_id}`}
          >
            <strong>JOB</strong><span>#{worker.current_job_id.slice(0, 4)}</span>
            <small>RUNNING</small>
          </div>
          : state === "BUSY" ? <span className="flow-placeholder">Active job not visible</span>
          : <span className="flow-idle">{state === "UNKNOWN" ? "Status unknown" : "IDLE"}</span>}
      </div>
    </article>
  );
}

export function OperationsConsole({ enabled, token, onJobCompleted, onJobsChanged }: OperationsConsoleProps) {
  const [snapshot, setSnapshot] = useState<OperationsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [completedTransitions, setCompletedTransitions] = useState<OperationsJob[]>([]);
  const previousJobs = useRef(new Map<string, OperationsJob>());
  const completedCallback = useRef(onJobCompleted);
  completedCallback.current = onJobCompleted;

  useEffect(() => {
    if (!enabled) {
      setSnapshot(null); setError(null); setLoading(false); setCompletedTransitions([]);
      previousJobs.current.clear();
      return undefined;
    }

    let disposed = false;
    let requestActive = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const completionTimers: number[] = [];
    let hasFlow = false;
    previousJobs.current.clear();
    setSnapshot(null);
    setCompletedTransitions([]);
    setLoading(true);

    const applySnapshot = (next: OperationsSummary) => {
      const nextJobs = new Map(next.visible_jobs.map((job) => [job.job_id, job]));
      const newlyCompleted = next.visible_jobs.filter((job) => {
        const previous = previousJobs.current.get(job.job_id);
        return previous !== undefined && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(previous.state)
          && (job.state === "SUCCEEDED" || job.state === "FAILED" || job.state === "CANCELLED");
      });
      previousJobs.current = nextJobs;
      setSnapshot((current) => current ? { ...current, generated_at: next.generated_at,
        visible_jobs: next.visible_jobs, visible_jobs_status: next.visible_jobs_status } : next);
      if (newlyCompleted.length > 0) {
        setCompletedTransitions((current) => [
          ...current.filter((item) => !newlyCompleted.some((job) => job.job_id === item.job_id)),
          ...newlyCompleted,
        ]);
        newlyCompleted.forEach((job) => completedCallback.current?.(job.job_id));
        completionTimers.push(window.setTimeout(() => {
          if (!disposed) {
            const completedIds = new Set(newlyCompleted.map((job) => job.job_id));
            setCompletedTransitions((current) =>
              current.filter((job) => !completedIds.has(job.job_id)),
            );
          }
        }, COMPLETION_DISPLAY_MS));
      }
    };

    const refresh = async () => {
      if (disposed || requestActive) return;
      requestActive = true;
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 10_000);
      try {
        const next = await getOperationsSummary(token, controller.signal);
        if (!disposed) {
          if (!hasFlow) {
            applySnapshot(next);
          }
          setSnapshot((current) => ({ ...next,
            visible_jobs: current?.visible_jobs ?? next.visible_jobs,
            visible_jobs_status: current?.visible_jobs_status ?? next.visible_jobs_status,
          }));
          setError(null);
        }
      } catch (requestError) {
        if (!disposed && !(requestError instanceof DOMException && requestError.name === "AbortError")) {
          setError(requestError instanceof Error ? requestError.message : "Operations data is unavailable.");
        }
      } finally {
        window.clearTimeout(timeout);
        requestActive = false;
        if (!disposed) {
          setLoading(false);
          timer = window.setTimeout(() => void refresh(), REFRESH_INTERVAL_MS);
        }
      }
    };

    void refresh();
    const stopFlow = poll((signal) => getJobFlow(token, signal), (next) => {
      hasFlow = true;
      applySnapshot(next);
    }, () => {
      setSnapshot((current) => current ? { ...current, visible_jobs_status: "UNAVAILABLE" } : current);
    }, 500);
    return () => {
      disposed = true;
      stopFlow();
      if (timer !== undefined) window.clearTimeout(timer);
      completionTimers.forEach((completionTimer) => window.clearTimeout(completionTimer));
      controller?.abort();
      requestActive = false;
    };
  }, [enabled, token]);

  const generatedAt = useMemo(() => snapshot === null ? null
    : new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date(snapshot.generated_at)),
  [snapshot]);
  const queueJobs = snapshot?.visible_jobs.filter(
    (job) => job.state === "ACCEPTED" || job.state === "QUEUED",
  ) ?? [];
  const retryJobs = snapshot?.visible_jobs.filter((job) => job.state === "RETRY_WAIT") ?? [];
  const runningJobs = snapshot?.visible_jobs.filter((job) => job.state === "RUNNING") ?? [];
  const workerKeys = new Set(snapshot?.workers.items.flatMap((worker) => {
    const key = workerIdentity(worker);
    return key === null ? [] : [key];
  }) ?? []);
  const jobsByWorker = new Map<string, OperationsJob[]>();
  runningJobs.forEach((job) => {
    if (job.worker_key !== null && workerKeys.has(job.worker_key)) {
      jobsByWorker.set(job.worker_key, [...(jobsByWorker.get(job.worker_key) ?? []), job]);
    }
  });
  const unassignedJobs = runningJobs.filter(
    (job) => job.worker_key === null || !workerKeys.has(job.worker_key),
  );

  return (
    <section className="operations-console" aria-labelledby="operations-heading">
      <ComputeDemo enabled={enabled} token={token} onJobsChanged={onJobsChanged} />
      <div className="operations-heading">
        <div><p className="eyebrow">Live operations</p><h2 id="operations-heading">Job flow</h2></div>
        <div className="operations-refresh" aria-live="polite">
          {error && snapshot ? <span className="operations-stale">STALE</span> : null}
          {generatedAt ? <span>Updated {generatedAt}</span> : <span>Job flow every 0.5s · monitoring every 3s</span>}
        </div>
      </div>
      {!enabled ? <div className="operations-empty">Connect to load live operations data.</div>
        : loading && snapshot === null ? <div className="operations-empty" role="status">Loading operations…</div>
        : snapshot === null ? <div className="operations-error" role="alert">{error ?? "Operations data is unavailable."}</div>
        : <>
          {error && <div className="operations-warning" role="status">Refresh failed. Showing the last successful snapshot.</div>}
          {snapshot.visible_jobs_status !== "AVAILABLE" && <div className="operations-warning" role="status">
            Job flow unavailable — showing last known states.
          </div>}
          <QueueLane jobs={queueJobs} ready={snapshot.rabbitmq.messages_ready}
            unacknowledged={snapshot.rabbitmq.messages_unacknowledged}
            visibleStatus={snapshot.visible_jobs_status} />
          <div className="flow-dependency-line">
            <span className={`availability availability-${snapshot.rabbitmq.status.toLowerCase()}`}>
              Queue {dependencyLabel(snapshot.rabbitmq.status)}
            </span>
            <span>{snapshot.rabbitmq.consumer_count ?? "Unavailable"} consumers</span>
            <span>System counts · visitor-visible blocks</span>
          </div>
          {retryJobs.length > 0 && <section className="retry-lane" aria-label="Jobs waiting to retry">
            <div className="flow-section-heading"><div><strong>Retry wait</strong><span>Backoff or recovery pending</span></div></div>
            <div className="retry-track">
              {retryJobs.slice(0, MAX_VISIBLE_JOBS).map((job) => <JobBlock job={job} key={job.job_id}
                note={job.attempt_state === "ABANDONED" ? "INTERRUPTED" : "WAITING"} />)}
              {retryJobs.length > MAX_VISIBLE_JOBS && <span className="flow-overflow">+{retryJobs.length - MAX_VISIBLE_JOBS} more</span>}
            </div>
          </section>}
          <section className="worker-pool" aria-label="Discovered workers">
            <div className="flow-section-heading"><div><strong>Workers</strong><span>{snapshot.workers.items.length} discovered</span></div></div>
            {snapshot.workers.items.length === 0
              ? <div className="workers-empty">{snapshot.workers.message ?? "No workers responded."}</div>
              : <div className="flow-worker-grid">{snapshot.workers.items.map((worker, index) => {
                const key = workerIdentity(worker);
                const assigned = key ? jobsByWorker.get(key) ?? [] : [];
                const known = snapshot.visible_jobs.find((job) => job.job_id === worker.current_job_id);
                return <WorkerCard worker={{ ...worker,
                  activity: assigned.length ? "BUSY" : worker.activity,
                  current_job_id: known && known.state !== "RUNNING" ? null : worker.current_job_id,
                }} index={index}
                  jobs={key ? jobsByWorker.get(key) ?? [] : []} key={key ?? index} />;
              })}</div>}
          </section>
          {unassignedJobs.length > 0 && <section className="unassigned-lane" aria-label="Unassigned running jobs">
            <strong>Running · worker correlation unavailable</strong>
            <div>{unassignedJobs.map((job) => <JobBlock job={job} note="RUNNING" key={job.job_id} />)}</div>
          </section>}
          {completedTransitions.length > 0 && <section className="completion-lane" aria-label="Recently completed jobs">
            <span>Completion</span>
            <div>{completedTransitions.map((job) => <JobBlock job={job} note={job.state} key={job.job_id} />)}</div>
          </section>}
          <section className="database-summary" aria-label="Persisted job counts">
            <div className="flow-section-heading"><div><strong>Persisted jobs</strong><span>PostgreSQL</span></div>
              <span className={`availability availability-${snapshot.postgresql.status.toLowerCase()}`}>
                {dependencyLabel(snapshot.postgresql.status)}
              </span></div>
            {snapshot.postgresql.job_counts === null
              ? <span className="dependency-unavailable">{snapshot.postgresql.message ?? "Job counts unavailable"}</span>
              : <div className="state-counts">{JOB_STATE_ORDER.map((state) => <span key={state}>
                <small>{state.replace("_", " ")}</small>
                <strong>{snapshot.postgresql.job_counts?.[state] ?? 0}</strong>
              </span>)}</div>}
          </section>
        </>}
    </section>
  );
}
