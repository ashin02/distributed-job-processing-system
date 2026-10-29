import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getJob,
  JobDetail,
  JobListItem,
  JobState,
  listJobs,
  submitJob,
} from "./api";

const PAGE_SIZE = 10;
const POLL_INTERVAL_MS = 5_000;
const ACTIVE_POLL_INTERVAL_MS = 1_000;
const TOKEN_STORAGE_KEY = "computegrid-development-token";
const ACTIVE_JOB_STATES = new Set<JobState>(["ACCEPTED", "QUEUED", "RUNNING", "RETRY_WAIT"]);

type TimelineStageState = "complete" | "active" | "pending" | "failed";

interface TimelineStage {
  label: string;
  timestamp: string | null;
  duration: string | null;
  state: TimelineStageState;
}

function formatTimestamp(value: string | null): string {
  if (value === null) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function formatDuration(start: string | null, end: string | null): string | null {
  if (start === null || end === null) return null;
  const milliseconds = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "An unexpected error occurred.";
}

function StatusBadge({ state }: { state: JobState }) {
  return <span className={`status status-${state.toLowerCase()}`}>{state}</span>;
}

function JsonBlock({ value, empty }: { value: unknown; empty: string }) {
  if (value === null || value === undefined) return <p className="muted compact">{empty}</p>;
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

function ExecutionTimeline({ job }: { job: JobDetail }) {
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.state);
  const currentIndex =
    job.state === "ACCEPTED" ? 0 : job.state === "QUEUED" ? 1 : terminal ? 3 : 2;
  const terminalLabel = terminal ? job.state : "SUCCEEDED / FAILED";
  const timestamps = [job.created_at, job.queued_at, job.started_at, job.completed_at];
  const durations = [
    null,
    formatDuration(job.queued_at, job.started_at),
    formatDuration(job.started_at, job.completed_at),
    formatDuration(job.created_at, job.completed_at),
  ];
  const labels = ["ACCEPTED", "QUEUED", "RUNNING", terminalLabel];
  const stages: TimelineStage[] = labels.map((label, index) => {
    let state: TimelineStageState = "pending";
    if (index < currentIndex) state = "complete";
    if (index === currentIndex) state = terminal && job.state === "FAILED" ? "failed" : "active";
    if (terminal && index === currentIndex && job.state !== "FAILED") state = "complete";
    return { label, timestamp: timestamps[index] ?? null, duration: durations[index] ?? null, state };
  });

  return (
    <section className="execution-timeline" aria-labelledby="execution-timeline-heading">
      <div className="timeline-heading">
        <div>
          <p className="eyebrow">Lifecycle</p>
          <h3 id="execution-timeline-heading">Execution timeline</h3>
        </div>
        {ACTIVE_JOB_STATES.has(job.state) && (
          <span className="live-refresh"><span aria-hidden="true" />Live · refreshes every 1s</span>
        )}
      </div>
      <ol aria-label="Execution timeline" className="timeline-stages">
        {stages.map((stage) => (
          <li
            className={`timeline-stage timeline-stage-${stage.state}`}
            data-stage-state={stage.state}
            key={stage.label}
          >
            <span className="timeline-marker" aria-hidden="true">
              {stage.state === "complete" ? "✓" : stage.state === "failed" ? "!" : ""}
            </span>
            <div className="timeline-stage-copy">
              <strong>{stage.label}</strong>
              <time dateTime={stage.timestamp ?? undefined}>
                {stage.timestamp === null ? "Awaiting transition" : formatTimestamp(stage.timestamp)}
              </time>
              {stage.duration !== null && (
                <span>{stage.label === terminalLabel ? "Total" : "Duration"} · {stage.duration}</span>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export default function App() {
  const storedToken = sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  const [tokenDraft, setTokenDraft] = useState(storedToken);
  const [token, setToken] = useState(storedToken);
  const [samples, setSamples] = useState("1000000");
  const [seed, setSeed] = useState("42");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  const connected = token.length > 0;
  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selectedSummary = useMemo(
    () => jobs.find((job) => job.job_id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );
  const selectedState =
    detail?.job_id === selectedJobId ? detail.state : selectedSummary?.state;
  const selectedJobIsActive = selectedState !== undefined && ACTIVE_JOB_STATES.has(selectedState);

  const loadJobs = useCallback(
    async (targetOffset = offset, quiet = false) => {
      if (!token) return;
      if (!quiet) setListLoading(true);
      setListError(null);
      try {
        const page = await listJobs(token, PAGE_SIZE, targetOffset);
        setJobs(page.items);
        setTotal(page.total);
      } catch (error) {
        setListError(errorMessage(error));
      } finally {
        if (!quiet) setListLoading(false);
      }
    },
    [offset, token],
  );

  const loadDetail = useCallback(
    async (jobId: string, quiet = false) => {
      if (!token) return;
      if (!quiet) setDetailLoading(true);
      setDetailError(null);
      try {
        setDetail(await getJob(token, jobId));
      } catch (error) {
        setDetailError(errorMessage(error));
      } finally {
        if (!quiet) setDetailLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    if (token) void loadJobs();
    else {
      setJobs([]);
      setTotal(0);
      setDetail(null);
    }
  }, [loadJobs, token]);

  useEffect(() => {
    if (selectedJobId && token) void loadDetail(selectedJobId);
  }, [loadDetail, selectedJobId, token]);

  useEffect(() => {
    if (!polling || !token || selectedJobIsActive) return undefined;
    const timer = window.setInterval(() => {
      void loadJobs(offset, true);
      if (selectedJobId) void loadDetail(selectedJobId, true);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadDetail, loadJobs, offset, polling, selectedJobId, selectedJobIsActive, token]);

  useEffect(() => {
    if (!token || !selectedJobId || !selectedJobIsActive) return undefined;
    const timer = window.setInterval(() => {
      void loadDetail(selectedJobId, true);
      void loadJobs(offset, true);
    }, ACTIVE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [loadDetail, loadJobs, offset, selectedJobId, selectedJobIsActive, token]);

  function connect(event: FormEvent) {
    event.preventDefault();
    const nextToken = tokenDraft.trim();
    if (!nextToken) return;
    sessionStorage.setItem(TOKEN_STORAGE_KEY, nextToken);
    setToken(nextToken);
    setOffset(0);
    setNotice("Development token connected for this browser session.");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const sampleCount = Number(samples);
    const seedValue = Number(seed);
    if (!Number.isInteger(sampleCount) || sampleCount < 1 || sampleCount > 10_000_000) {
      setSubmitError("Samples must be an integer between 1 and 10,000,000.");
      return;
    }
    if (!Number.isInteger(seedValue)) {
      setSubmitError("Seed must be an integer.");
      return;
    }

    setSubmitting(true);
    setSubmitError(null);
    setNotice(null);
    try {
      const accepted = await submitJob(
        token,
        { samples: sampleCount, seed: seedValue },
        idempotencyKey,
      );
      setNotice(`Job ${accepted.job_id} was durably accepted for asynchronous execution.`);
      setIdempotencyKey(crypto.randomUUID());
      setOffset(0);
      setSelectedJobId(accepted.job_id);
      await loadJobs(0);
    } catch (error) {
      setSubmitError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">CG</span>
          <div>
            <p className="eyebrow">Local operations console</p>
            <h1>ComputeGrid</h1>
          </div>
        </div>
        <form className="auth-form" onSubmit={connect}>
          <label htmlFor="api-token">Development API token</label>
          <div className="inline-control">
            <input
              id="api-token"
              type="password"
              value={tokenDraft}
              onChange={(event) => setTokenDraft(event.target.value)}
              placeholder="Paste local token"
              autoComplete="off"
            />
            <button type="submit" className="button-secondary" disabled={!tokenDraft.trim()}>
              {connected ? "Reconnect" : "Connect"}
            </button>
          </div>
          <span className={connected ? "connection connected" : "connection"}>
            {connected ? "Connected" : "Token required"}
          </span>
        </form>
      </header>

      <main>
        <section className="intro panel">
          <div>
            <p className="eyebrow">Distributed execution</p>
            <h2>Submit, track, and inspect deterministic compute jobs.</h2>
            <p className="muted">
              Jobs move from durable acceptance through the RabbitMQ-backed worker pipeline.
              Active jobs refresh automatically each second; optional polling keeps idle history current.
            </p>
          </div>
          <div className="metric">
            <span>Visible jobs</span>
            <strong>{connected ? total : "—"}</strong>
          </div>
        </section>

        {notice && <div className="notice success" role="status">{notice}</div>}

        <div className="workspace-grid">
          <section className="panel submit-panel">
            <div className="section-heading">
              <div>
                <p className="eyebrow">New job</p>
                <h2>Monte Carlo π</h2>
              </div>
              <span className="execution-label">Async worker</span>
            </div>
            <form onSubmit={submit} className="stacked-form">
              <label>
                Samples
                <input
                  type="number"
                  min="1"
                  max="10000000"
                  step="1"
                  value={samples}
                  onChange={(event) => setSamples(event.target.value)}
                  disabled={!connected || submitting}
                />
                <span>1 to 10,000,000 deterministic samples</span>
              </label>
              <label>
                Seed
                <input
                  type="number"
                  step="1"
                  value={seed}
                  onChange={(event) => setSeed(event.target.value)}
                  disabled={!connected || submitting}
                />
                <span>Integer used to reproduce the workload</span>
              </label>
              <div className="idempotency-note">
                <span>Idempotency key</span>
                <code title={idempotencyKey}>{idempotencyKey}</code>
              </div>
              {submitError && <div className="notice error" role="alert">{submitError}</div>}
              <button type="submit" className="button-primary" disabled={!connected || submitting}>
                {submitting ? "Submitting…" : "Submit job"}
              </button>
            </form>
          </section>

          <section className="panel jobs-panel">
            <div className="section-heading jobs-heading">
              <div>
                <p className="eyebrow">Job ledger</p>
                <h2>Recent jobs</h2>
              </div>
              <div className="toolbar">
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={polling}
                    onChange={(event) => setPolling(event.target.checked)}
                    disabled={!connected}
                  />
                  Poll every 5s
                </label>
                <button
                  type="button"
                  className="button-secondary"
                  onClick={() => void loadJobs()}
                  disabled={!connected || listLoading}
                >
                  Refresh
                </button>
              </div>
            </div>

            {!connected && <div className="empty-state">Connect a development token to load jobs.</div>}
            {connected && listLoading && <div className="empty-state" role="status">Loading jobs…</div>}
            {connected && listError && <div className="notice error" role="alert">{listError}</div>}
            {connected && !listLoading && !listError && jobs.length === 0 && (
              <div className="empty-state">No jobs yet. Submit one to create the first record.</div>
            )}
            {jobs.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Job</th><th>Status</th><th>Created</th><th>Updated</th></tr>
                  </thead>
                  <tbody>
                    {jobs.map((job) => (
                      <tr key={job.job_id} className={job.job_id === selectedJobId ? "selected" : ""}>
                        <td>
                          <button className="job-link" onClick={() => setSelectedJobId(job.job_id)}>
                            <strong>{job.type}</strong>
                            <span>{job.job_id.slice(0, 12)}…</span>
                          </button>
                        </td>
                        <td><StatusBadge state={job.state} /></td>
                        <td>{formatTimestamp(job.created_at)}</td>
                        <td>{formatTimestamp(job.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="pagination">
              <button
                type="button"
                className="button-secondary"
                disabled={offset === 0 || listLoading}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </button>
              <span>Page {pageNumber} of {pageCount}</span>
              <button
                type="button"
                className="button-secondary"
                disabled={offset + PAGE_SIZE >= total || listLoading}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          </section>
        </div>

        <section className="panel detail-panel">
          <div className="section-heading jobs-heading">
            <div>
              <p className="eyebrow">Inspection</p>
              <h2>Job detail</h2>
            </div>
            <div className="toolbar">
              {(detail ?? selectedSummary) && <StatusBadge state={(detail ?? selectedSummary)!.state} />}
              <button
                type="button"
                className="button-secondary"
                onClick={() => selectedJobId && void loadDetail(selectedJobId)}
                disabled={!selectedJobId || detailLoading}
              >
                Refresh detail
              </button>
            </div>
          </div>
          {!selectedJobId && <div className="empty-state">Select a job from the ledger to inspect it.</div>}
          {selectedJobId && detailLoading && <div className="empty-state" role="status">Loading detail…</div>}
          {detailError && <div className="notice error" role="alert">{detailError}</div>}
          {detail && !detailLoading && (
            <div className="detail-content">
              <div className="detail-meta">
                <div><span>Job ID</span><code>{detail.job_id}</code></div>
                <div><span>Created</span><strong>{formatTimestamp(detail.created_at)}</strong></div>
                <div><span>Updated</span><strong>{formatTimestamp(detail.updated_at)}</strong></div>
                <div><span>Completed</span><strong>{formatTimestamp(detail.completed_at)}</strong></div>
              </div>
              <ExecutionTimeline job={detail} />
              <div className="detail-grid">
                <article><h3>Parameters</h3><JsonBlock value={detail.parameters} empty="No parameters." /></article>
                <article><h3>Result</h3><JsonBlock value={detail.result} empty="No result has been committed." /></article>
                <article><h3>Error</h3><JsonBlock value={detail.error} empty="No error recorded." /></article>
              </div>
              <div className="attempts">
                <h3>Attempts</h3>
                {detail.attempts.length === 0 ? (
                  <p className="muted">No execution attempt has been recorded yet.</p>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>#</th><th>Status</th><th>Worker</th><th>Started</th><th>Finished</th></tr></thead>
                      <tbody>
                        {detail.attempts.map((attempt) => (
                          <tr key={attempt.attempt_id}>
                            <td>{attempt.attempt_number}</td>
                            <td>{attempt.state}</td>
                            <td>{attempt.worker_id ?? "—"}</td>
                            <td>{formatTimestamp(attempt.started_at)}</td>
                            <td>{formatTimestamp(attempt.finished_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
