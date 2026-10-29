import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { getJob, listJobs, submitJob } from "./api";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getJob: vi.fn(),
    listJobs: vi.fn(),
    submitJob: vi.fn(),
  };
});

const job = {
  job_id: "01JCOMPUTEGRID000000000001",
  type: "monte_carlo_pi" as const,
  state: "ACCEPTED" as const,
  created_at: "2026-10-28T20:00:00Z",
  updated_at: "2026-10-28T20:00:00Z",
};

const detail = {
  ...job,
  parameters: { samples: 1000, seed: 42 },
  result: null,
  error: null,
  attempts: [],
  queued_at: null,
  started_at: null,
  completed_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listJobs).mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  vi.mocked(getJob).mockResolvedValue(detail);
  vi.mocked(submitJob).mockResolvedValue({
    job_id: job.job_id,
    state: "ACCEPTED",
    status_url: `/v1/jobs/${job.job_id}`,
  });
});

async function connect() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Development API token"), "test-token");
  await user.click(screen.getByRole("button", { name: "Connect" }));
  await waitFor(() => expect(listJobs).toHaveBeenCalledWith("test-token", 10, 0));
  return user;
}

describe("ComputeGrid dashboard", () => {
  it("requires a development token before submission", () => {
    render(<App />);

    expect(screen.getByText("Token required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit job" })).toBeDisabled();
    expect(screen.getByText(/RabbitMQ-backed worker pipeline/)).toBeInTheDocument();
  });

  it("submits a Monte Carlo job for asynchronous execution", async () => {
    render(<App />);
    const user = await connect();

    await user.click(screen.getByRole("button", { name: "Submit job" }));

    await waitFor(() => {
      expect(submitJob).toHaveBeenCalledWith(
        "test-token",
        { samples: 1_000_000, seed: 42 },
        expect.any(String),
      );
    });
    expect(await screen.findByText(/accepted for asynchronous execution/)).toBeInTheDocument();
    expect(getJob).toHaveBeenCalledWith("test-token", job.job_id);
  });

  it("loads a page and displays job detail", async () => {
    vi.mocked(listJobs).mockResolvedValue({ items: [job], total: 1, limit: 10, offset: 0 });
    render(<App />);
    const user = await connect();

    await user.click(await screen.findByRole("button", { name: /monte_carlo_pi/ }));

    expect(await screen.findByText(/No execution attempt/)).toBeInTheDocument();
    expect(screen.getByText(/"samples": 1000/)).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 1")).toBeInTheDocument();
  });

  it("displays a completed execution result and attempt", async () => {
    const completed = {
      ...detail,
      state: "SUCCEEDED" as const,
      result: { pi_estimate: 3.1412, samples: 1000, seed: 42 },
      queued_at: "2026-10-28T20:00:00.500Z",
      started_at: "2026-10-28T20:00:01Z",
      completed_at: "2026-10-28T20:00:02Z",
      attempts: [
        {
          attempt_id: "attempt-1",
          attempt_number: 1,
          state: "SUCCEEDED" as const,
          worker_id: "worker-1",
          lease_expires_at: null,
          started_at: "2026-10-28T20:00:01Z",
          finished_at: "2026-10-28T20:00:02Z",
          error: null,
          created_at: "2026-10-28T20:00:01Z",
          updated_at: "2026-10-28T20:00:02Z",
        },
      ],
    };
    vi.mocked(listJobs).mockResolvedValue({
      items: [{ ...job, state: "SUCCEEDED" }],
      total: 1,
      limit: 10,
      offset: 0,
    });
    vi.mocked(getJob).mockResolvedValue(completed);
    render(<App />);
    const user = await connect();

    await user.click(await screen.findByRole("button", { name: /monte_carlo_pi/ }));

    expect(await screen.findByText(/"pi_estimate": 3.1412/)).toBeInTheDocument();
    expect(screen.getByText("worker-1")).toBeInTheDocument();
    const timeline = screen.getByRole("list", { name: "Execution timeline" });
    const stages = within(timeline).getAllByRole("listitem");
    expect(stages).toHaveLength(4);
    expect(stages.every((stage) => stage.dataset.stageState === "complete")).toBe(true);
    expect(within(timeline).getByText("Total · 2.0 s")).toBeInTheDocument();
  });

  it("automatically refreshes an active job every second until completion", async () => {
    const queued = {
      ...detail,
      state: "QUEUED" as const,
      queued_at: "2026-10-28T20:00:00.500Z",
    };
    const succeeded = {
      ...queued,
      state: "SUCCEEDED" as const,
      started_at: "2026-10-28T20:00:01Z",
      completed_at: "2026-10-28T20:00:01.250Z",
      result: { pi_estimate: 3.14, samples: 1000, seed: 42 },
    };
    vi.mocked(listJobs).mockResolvedValue({
      items: [{ ...job, state: "QUEUED" }],
      total: 1,
      limit: 10,
      offset: 0,
    });
    vi.mocked(getJob).mockResolvedValueOnce(queued).mockResolvedValue(succeeded);
    render(<App />);
    const user = await connect();
    await user.click(await screen.findByRole("button", { name: /monte_carlo_pi/ }));

    expect(await screen.findByText(/Live · refreshes every 1s/)).toBeInTheDocument();
    await waitFor(() => expect(getJob).toHaveBeenCalledTimes(2), { timeout: 1_800 });
    expect(await screen.findByText(/"pi_estimate": 3.14/)).toBeInTheDocument();
    expect(screen.queryByText(/Live · refreshes every 1s/)).not.toBeInTheDocument();
  });

  it("renders a failed terminal stage without hiding completed stages", async () => {
    const failed = {
      ...detail,
      state: "FAILED" as const,
      queued_at: "2026-10-28T20:00:00.200Z",
      started_at: "2026-10-28T20:00:00.400Z",
      completed_at: "2026-10-28T20:00:00.700Z",
      error: { type: "ValueError", message: "computation failed" },
    };
    vi.mocked(listJobs).mockResolvedValue({
      items: [{ ...job, state: "FAILED" }],
      total: 1,
      limit: 10,
      offset: 0,
    });
    vi.mocked(getJob).mockResolvedValue(failed);
    render(<App />);
    const user = await connect();
    await user.click(await screen.findByRole("button", { name: /monte_carlo_pi/ }));

    const timeline = await screen.findByRole("list", { name: "Execution timeline" });
    const stages = within(timeline).getAllByRole("listitem");
    expect(stages.slice(0, 3).every((stage) => stage.dataset.stageState === "complete")).toBe(true);
    expect(within(timeline).getByText("FAILED").closest("li")).toHaveAttribute(
      "data-stage-state",
      "failed",
    );
  });

  it("shows list loading failures", async () => {
    vi.mocked(listJobs).mockRejectedValue(new Error("API unavailable"));
    render(<App />);

    await connect();

    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
  });
});
