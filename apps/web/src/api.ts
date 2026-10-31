export type JobState =
  | "ACCEPTED"
  | "QUEUED"
  | "RUNNING"
  | "RETRY_WAIT"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type AttemptState = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "ABANDONED";

export interface MonteCarloPiParameters {
  samples: number;
  seed: number;
}

export interface WeightedEdge {
  source: string;
  target: string;
  weight: number;
}

export interface WeightedMaxCutParameters {
  nodes: string[];
  edges: WeightedEdge[];
}

export interface QuadraticBias {
  source: string;
  target: string;
  bias: number;
}

export interface BQMParameters {
  vartype: "BINARY" | "SPIN";
  variables: string[];
  linear: Record<string, number>;
  quadratic: QuadraticBias[];
  offset: number;
}

export interface CQMConstraint {
  name: string;
  linear: Record<string, number>;
  sense: "<=" | ">=" | "==";
  rhs: number;
}

export interface CQMParameters {
  variables: string[];
  objective: {
    sense: "minimize" | "maximize";
    linear: Record<string, number>;
    offset?: number;
  };
  constraints: CQMConstraint[];
}

export type JobType = "monte_carlo_pi" | "weighted_max_cut" | "bqm" | "cqm";

export type JobCreateRequest =
  | { type: "monte_carlo_pi"; parameters: MonteCarloPiParameters }
  | { type: "weighted_max_cut"; parameters: WeightedMaxCutParameters }
  | { type: "bqm"; parameters: BQMParameters }
  | { type: "cqm"; parameters: CQMParameters };

export interface JobAccepted {
  job_id: string;
  state: JobState;
  status_url: string;
}

export interface JobListItem {
  job_id: string;
  type: JobType;
  state: JobState;
  created_at: string;
  updated_at: string;
}

export interface JobListPage {
  items: JobListItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobAttempt {
  attempt_id: string;
  attempt_number: number;
  state: AttemptState;
  worker_id: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface JobDetail {
  job_id: string;
  type: JobType;
  parameters: Record<string, unknown>;
  state: JobState;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  attempts: JobAttempt[];
  created_at: string;
  updated_at: string;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export type DependencyStatus = "AVAILABLE" | "UNAVAILABLE" | "UNKNOWN";
export type WorkerActivity = "IDLE" | "BUSY" | "UNKNOWN";

export interface RabbitMQOperations {
  status: DependencyStatus;
  queue_name?: string;
  messages_ready: number | null;
  messages_unacknowledged: number | null;
  consumer_count: number | null;
  message: string | null;
}

export interface OperationsWorker {
  worker_id?: string;
  label?: string;
  worker_key?: string;
  availability: DependencyStatus;
  activity: WorkerActivity;
  current_job_id?: string | null;
}

export interface WorkersOperations {
  status: DependencyStatus;
  items: OperationsWorker[];
  available?: number;
  idle?: number;
  busy?: number;
  message: string | null;
}

export interface PostgreSQLOperations {
  status: DependencyStatus;
  job_counts: Partial<Record<JobState, number>> | null;
  message: string | null;
}

export interface OperationsSummary {
  generated_at: string;
  rabbitmq: RabbitMQOperations;
  workers: WorkersOperations;
  postgresql: PostgreSQLOperations;
  visible_jobs_status: DependencyStatus;
  visible_jobs: OperationsJob[];
}

export interface OperationsJob {
  job_id: string;
  job_type: JobType;
  state: JobState;
  attempt_state: AttemptState | null;
  worker_key: string | null;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

async function request<T>(path: string, token: string | null, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body.detail)) {
        const validationMessages = body.detail.flatMap((item) => {
          if (typeof item !== "object" || item === null || !("msg" in item)) return [];
          return typeof item.msg === "string" ? [item.msg] : [];
        });
        if (validationMessages.length > 0) message = validationMessages.join("; ");
      }
    } catch {
      // Preserve the status-based fallback when the response is not JSON.
    }
    throw new ApiError(response.status, message);
  }

  return (await response.json()) as T;
}

export function submitJob(
  token: string | null,
  job: JobCreateRequest,
  idempotencyKey: string,
): Promise<JobAccepted> {
  return request<JobAccepted>("/v1/jobs", token, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(job),
  });
}

export function listJobs(token: string | null, limit: number, offset: number, signal?: AbortSignal): Promise<JobListPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<JobListPage>(`/v1/jobs?${query}`, token, { signal });
}

export function getJob(token: string | null, jobId: string, signal?: AbortSignal): Promise<JobDetail> {
  return request<JobDetail>(`/v1/jobs/${encodeURIComponent(jobId)}`, token, { signal });
}

export function getOperationsSummary(
  token: string | null,
  signal?: AbortSignal,
): Promise<OperationsSummary> {
  return request<OperationsSummary>("/v1/operations/summary", token, { signal });
}

export function getJobFlow(token: string | null, signal?: AbortSignal): Promise<OperationsSummary> {
  return request<OperationsSummary>("/v1/operations/summary?flow_only=true", token, { signal });
}

export interface DemoRun {
  id: string;
  started_at: string;
  ends_at: string;
  remaining_seconds: number;
  feed_status: "FEEDING" | "COMPLETE";
  submitted: number;
  counts: Record<JobState, number>;
  execution_complete: boolean;
  reason: string | null;
}

export function latestDemoRun(token: string | null, signal?: AbortSignal): Promise<DemoRun | null> {
  return request<DemoRun | null>("/v1/demo-runs/latest", token, { signal });
}

export function startDemoRun(token: string | null, key: string, signal?: AbortSignal): Promise<DemoRun> {
  return request<DemoRun>("/v1/demo-runs", token, {
    method: "POST", signal, headers: { "Idempotency-Key": key },
  });
}
