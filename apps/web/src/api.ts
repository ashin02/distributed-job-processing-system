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

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(
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

export function listJobs(token: string | null, limit: number, offset: number): Promise<JobListPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<JobListPage>(`/v1/jobs?${query}`, token);
}

export function getJob(token: string | null, jobId: string): Promise<JobDetail> {
  return request<JobDetail>(`/v1/jobs/${encodeURIComponent(jobId)}`, token);
}
