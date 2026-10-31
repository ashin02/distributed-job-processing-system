import { afterEach, describe, expect, it, vi } from "vitest";

import { API_BASE_URL, ApiError, getOperationsSummary, listJobs, submitJob } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client", () => {
  it("maps the operations snapshot and preserves anonymous cookie credentials", async () => {
    const payload = {
      generated_at: "2026-10-30T20:00:00Z",
      rabbitmq: {
        status: "AVAILABLE", messages_ready: 5, messages_unacknowledged: 2,
        consumer_count: 4, message: null,
      },
      workers: {
        status: "AVAILABLE",
        items: [{
          worker_key: "worker-safe-a", label: "Worker 00AF",
          availability: "AVAILABLE", activity: "BUSY",
        }],
        available: 1, idle: 0, busy: 1, message: null,
      },
      postgresql: { status: "AVAILABLE", job_counts: { RUNNING: 1 }, message: null },
      visible_jobs_status: "AVAILABLE",
      visible_jobs: [{
        job_id: "a2cc9b84-8fe2-43b5-a596-3c166a344437",
        job_type: "monte_carlo_pi", state: "RUNNING",
        attempt_state: "RUNNING", worker_key: "worker-safe-a",
      }],
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getOperationsSummary(null);

    expect(result).toEqual(payload);
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.credentials).toBe("include");
    expect(request.headers).not.toHaveProperty("Authorization");
  });

  it("uses credentialed cookies without exposing an authorization token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 10, offset: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await listJobs(null, 10, 0);

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE_URL}/v1/jobs?limit=10&offset=0`,
      expect.any(Object),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.credentials).toBe("include");
    expect(request.headers).not.toHaveProperty("Authorization");
  });

  it("submits an authenticated idempotent job", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          job_id: "job-1",
          state: "ACCEPTED",
          status_url: "/v1/jobs/job-1",
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await submitJob(
      "secret",
      { type: "monte_carlo_pi", parameters: { samples: 1000, seed: 42 } },
      "request-1",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE_URL}/v1/jobs`,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          Authorization: "Bearer secret",
          "Idempotency-Key": "request-1",
        }),
      }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({
      type: "monte_carlo_pi",
      parameters: { samples: 1000, seed: 42 },
    });
  });

  it("submits a Weighted Max-Cut graph without changing its structure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ job_id: "job-2", state: "ACCEPTED", status_url: "/v1/jobs/job-2" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const graph = {
      nodes: ["a", "b"],
      edges: [{ source: "a", target: "b", weight: 3 }],
    };

    await submitJob("secret", { type: "weighted_max_cut", parameters: graph }, "request-2");

    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(request.body))).toEqual({
      type: "weighted_max_cut",
      parameters: graph,
    });
  });

  it("submits generic BQM and CQM models without transformation", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(
        JSON.stringify({ job_id: "job-3", state: "ACCEPTED", status_url: "/v1/jobs/job-3" }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const bqm = {
      vartype: "SPIN" as const,
      variables: ["s0", "s1"],
      linear: { s0: 1 },
      quadratic: [{ source: "s0", target: "s1", bias: -2 }],
      offset: 0,
    };
    const cqm = {
      variables: ["x"],
      objective: { sense: "maximize" as const, linear: { x: 1 } },
      constraints: [{ name: "bound", linear: { x: 1 }, sense: "<=" as const, rhs: 1 }],
    };

    await submitJob("secret", { type: "bqm", parameters: bqm }, "request-3");
    await submitJob("secret", { type: "cqm", parameters: cqm }, "request-4");

    const first = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const second = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(JSON.parse(String(first.body))).toEqual({ type: "bqm", parameters: bqm });
    expect(JSON.parse(String(second.body))).toEqual({ type: "cqm", parameters: cqm });
  });

  it("passes pagination and surfaces API error details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid or missing API token" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listJobs("bad-token", 10, 20)).rejects.toEqual(
      new ApiError(401, "Invalid or missing API token"),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE_URL}/v1/jobs?limit=10&offset=20`,
      expect.any(Object),
    );
  });
});
