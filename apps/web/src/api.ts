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

export interface JobAccepted {
  job_id: string;
  state: JobState;
  status_url: string;
}

export interface JobListItem {
  job_id: string;
  type: "monte_carlo_pi";
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
  type: "monte_carlo_pi";
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

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        message = body.detail;
      }
    } catch {
      // Preserve the status-based fallback when the response is not JSON.
    }
    throw new ApiError(response.status, message);
  }

  return (await response.json()) as T;
}

export function submitJob(
  token: string,
  parameters: MonteCarloPiParameters,
  idempotencyKey: string,
): Promise<JobAccepted> {
  return request<JobAccepted>("/v1/jobs", token, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ type: "monte_carlo_pi", parameters }),
  });
}

export function listJobs(token: string, limit: number, offset: number): Promise<JobListPage> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<JobListPage>(`/v1/jobs?${query}`, token);
}

export function getJob(token: string, jobId: string): Promise<JobDetail> {
  return request<JobDetail>(`/v1/jobs/${encodeURIComponent(jobId)}`, token);
}
