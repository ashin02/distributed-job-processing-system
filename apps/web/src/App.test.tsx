import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
vi.mock("./ComputeDemo", () => ({ ComputeDemo: () => null }));
import { getJob, getOperationsSummary, listJobs, submitJob } from "./api";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getJob: vi.fn(),
    getOperationsSummary: vi.fn(),
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
  vi.mocked(listJobs).mockReset().mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  vi.mocked(getJob).mockReset().mockResolvedValue(detail);
  vi.mocked(submitJob).mockReset().mockResolvedValue({
    job_id: job.job_id,
    state: "ACCEPTED",
    status_url: `/v1/jobs/${job.job_id}`,
  });
  vi.mocked(getOperationsSummary).mockReset().mockResolvedValue({
    generated_at: "2026-10-30T20:00:00Z",
    rabbitmq: {
      status: "AVAILABLE", messages_ready: 0, messages_unacknowledged: 0,
      consumer_count: 0, message: null,
    },
    workers: { status: "AVAILABLE", items: [], message: null },
    postgresql: { status: "AVAILABLE", job_counts: {}, message: null },
    visible_jobs_status: "AVAILABLE",
    visible_jobs: [],
  });
});

async function connect() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Development API token"), "test-token");
  await user.click(screen.getByRole("button", { name: "Connect" }));
  await waitFor(() => expect(listJobs).toHaveBeenCalledWith("test-token", 10, 0, expect.any(AbortSignal)));
  return user;
}

describe("ComputeGrid dashboard", () => {
  it("starts an isolated browser demo without a token when demo mode is enabled", async () => {
    render(<App demoMode />);

    expect(screen.getByText("Anonymous demo")).toBeInTheDocument();
    expect(screen.getByText("Private browser session")).toBeInTheDocument();
    expect(screen.queryByLabelText("Development API token")).not.toBeInTheDocument();
    await waitFor(() => expect(listJobs).toHaveBeenCalledWith(null, 10, 0, expect.any(AbortSignal)));
    expect(screen.getByRole("button", { name: "Submit job" })).toBeEnabled();
  });

  it("requires a development token before submission", () => {
    render(<App />);

    expect(screen.getByText("Token required")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Submit job" })).toBeDisabled();
    expect(screen.getByText(/classical CPU/)).toBeInTheDocument();
  });

  it("submits a Monte Carlo job for asynchronous execution", async () => {
    render(<App />);
    const user = await connect();

    await user.click(screen.getByRole("button", { name: "Submit job" }));

    await waitFor(() => {
      expect(submitJob).toHaveBeenCalledWith(
        "test-token",
        { type: "monte_carlo_pi", parameters: { samples: 1_000_000, seed: 42 } },
        expect.any(String),
      );
    });
    expect(await screen.findByText(/accepted for asynchronous execution/)).toBeInTheDocument();
    expect(getJob).toHaveBeenCalledWith("test-token", job.job_id, expect.any(AbortSignal));
  });

  it("loads a page and displays job detail", async () => {
    vi.mocked(listJobs).mockResolvedValue({ items: [job], total: 1, limit: 10, offset: 0 });
    render(<App />);
    const user = await connect();

    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

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

    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

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
    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

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
    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

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

  it("submits the example Weighted Max-Cut graph", async () => {
    render(<App />);
    const user = await connect();

    await user.click(screen.getByRole("button", { name: /^Weighted Max-Cut/ }));
    await user.click(screen.getByRole("button", { name: "Submit job" }));

    await waitFor(() => {
      expect(submitJob).toHaveBeenCalledWith(
        "test-token",
        expect.objectContaining({
          type: "weighted_max_cut",
          parameters: expect.objectContaining({ nodes: ["a", "b", "c", "d"] }),
        }),
        expect.any(String),
      );
    });
  });

  it("renders the graph, partition, QUBO, and abandoned attempt", async () => {
    const optimizationJob = {
      ...job,
      type: "weighted_max_cut" as const,
      state: "SUCCEEDED" as const,
    };
    vi.mocked(listJobs).mockResolvedValue({
      items: [optimizationJob], total: 1, limit: 10, offset: 0,
    });
    vi.mocked(getJob).mockResolvedValue({
      ...detail,
      ...optimizationJob,
      parameters: {
        nodes: ["a", "b"],
        edges: [{ source: "a", target: "b", weight: 3 }],
      },
      result: {
        cut_weight: 3,
        partition: { side_a: ["a"], side_b: ["b"] },
        qubo: {
          linear: [{ variable: "a", bias: -3 }, { variable: "b", bias: -3 }],
          quadratic: [{ source: "a", target: "b", bias: 6 }],
          energy_relation: "energy = -cut_weight",
        },
        solver: {
          name: "exact_classical_enumeration",
          optimal: true,
          assignments_evaluated: 2,
          symmetry_breaking: "a fixed to side_a",
        },
      },
      queued_at: "2026-10-28T20:00:00.100Z",
      started_at: "2026-10-28T20:00:00.200Z",
      completed_at: "2026-10-28T20:00:01Z",
      attempts: [{
        attempt_id: "attempt-abandoned",
        attempt_number: 1,
        state: "ABANDONED" as const,
        worker_id: "worker-1",
        lease_expires_at: null,
        started_at: "2026-10-28T20:00:00.200Z",
        finished_at: "2026-10-28T20:00:00.500Z",
        error: { type: "WorkerLeaseExpired", message: "lease expired" },
        created_at: "2026-10-28T20:00:00.200Z",
        updated_at: "2026-10-28T20:00:00.500Z",
      }],
    });
    render(<App />);
    const user = await connect();
    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

    expect(await screen.findByRole("img", { name: "Weighted graph" })).toBeInTheDocument();
    expect(screen.getByText("Optimal cut").parentElement).toHaveTextContent("3");
    expect(screen.getByText("Q[a,b] = 6")).toBeInTheDocument();
    expect(screen.getByText("exact_classical_enumeration")).toBeInTheDocument();
    expect(screen.getByText("ABANDONED")).toBeInTheDocument();
    expect(screen.getByText("lease expired")).toBeInTheDocument();
  });

  it("offers four selectable jobs with accessible educational panels", async () => {
    render(<App />);
    const user = userEvent.setup();

    expect(screen.getByRole("button", { name: /^Monte Carlo π/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Weighted Max-Cut/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^BQM/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^CQM/ })).toBeInTheDocument();

    const infoButton = screen.getByRole("button", { name: "About BQM" });
    expect(infoButton).toHaveAttribute("aria-expanded", "false");
    await user.click(infoButton);
    expect(infoButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("What it represents")).toBeInTheDocument();
    expect(screen.getByText("Example applications")).toBeInTheDocument();
    expect(screen.getByText("How this demo solves it")).toBeInTheDocument();
    expect(screen.getByText("Beyond this demo")).toBeInTheDocument();
    expect(screen.getByText(/Scheduling, feature selection/)).toBeInTheDocument();
    expect(screen.getByText(/Exact enumeration doubles with each variable/)).toBeInTheDocument();
    expect(screen.getAllByText(/All jobs run locally on classical CPU workers/)).toHaveLength(1);
    expect(screen.queryByText("D-Wave relationship")).not.toBeInTheDocument();
    expect(screen.queryByText("Limitations")).not.toBeInTheDocument();
  });

  it.each([
    ["BQM", "bqm", "Binary Quadratic Model JSON", "BINARY"],
    ["CQM", "cqm", "Constrained Quadratic Model JSON", undefined],
  ] as const)("submits the example %s model", async (label, type, editorLabel, vartype) => {
    render(<App />);
    const user = await connect();

    await user.click(screen.getByRole("button", { name: new RegExp(`^${label}`) }));
    expect(screen.getByLabelText(editorLabel)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Submit job" }));

    await waitFor(() => {
      expect(submitJob).toHaveBeenCalledWith(
        "test-token",
        expect.objectContaining({
          type,
          parameters: expect.objectContaining(
            vartype ? { vartype, variables: ["x", "y", "z"] } : { variables: ["x", "y", "z"] },
          ),
        }),
        expect.any(String),
      );
    });
  });

  it("renders minimum-energy BQM assignments", async () => {
    const bqmJob = { ...job, type: "bqm" as const, state: "SUCCEEDED" as const };
    vi.mocked(listJobs).mockResolvedValue({ items: [bqmJob], total: 1, limit: 10, offset: 0 });
    vi.mocked(getJob).mockResolvedValue({
      ...detail,
      ...bqmJob,
      parameters: { vartype: "SPIN", variables: ["s0", "s1"] },
      result: {
        assignment: { s0: -1, s1: -1 },
        minimum_energy: -2,
        vartype: "SPIN",
        solver: { name: "exact_classical_enumeration", assignments_evaluated: 4 },
      },
    });
    render(<App />);
    const user = await connect();
    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

    expect(await screen.findByText("Minimum energy")).toBeInTheDocument();
    expect(screen.getByText("SPIN variables")).toBeInTheDocument();
    expect(screen.getByText("s0")).toBeInTheDocument();
  });

  it("distinguishes an infeasible CQM solution from failed execution", async () => {
    const cqmJob = { ...job, type: "cqm" as const, state: "SUCCEEDED" as const };
    vi.mocked(listJobs).mockResolvedValue({ items: [cqmJob], total: 1, limit: 10, offset: 0 });
    vi.mocked(getJob).mockResolvedValue({
      ...detail,
      ...cqmJob,
      parameters: { variables: ["x"], objective: {}, constraints: [] },
      result: {
        status: "INFEASIBLE",
        assignment: null,
        objective_value: null,
        feasible: false,
        optimal: false,
        solver: {
          name: "exact_classical_enumeration",
          assignments_evaluated: 2,
          feasible_assignments: 0,
        },
      },
    });
    render(<App />);
    const user = await connect();
    await user.click(await screen.findByRole("button", { name: /01JCOMPUTE/ }));

    expect(await screen.findByText("No feasible assignment exists.")).toBeInTheDocument();
    expect(screen.getByText(/worker completed successfully/)).toBeInTheDocument();
    expect(screen.getAllByText("SUCCEEDED").length).toBeGreaterThan(0);
  });
});
