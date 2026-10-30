import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, listJobs, submitJob } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API client", () => {
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
      "http://localhost:8000/v1/jobs",
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
      "http://localhost:8000/v1/jobs?limit=10&offset=20",
      expect.any(Object),
    );
  });
});
