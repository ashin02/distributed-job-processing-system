import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OperationsSummary } from "./api";
import { OperationsConsole } from "./OperationsConsole";
import { getJobFlow, getOperationsSummary } from "./api";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");

  return {
    ...actual,
    getOperationsSummary: vi.fn(),
    getJobFlow: vi.fn(
      (_token: string | null, signal: AbortSignal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    ),
  };
});

const jobId = "a2cc9b84-8fe2-43b5-a596-3c166a344437";
vi.mock("./ComputeDemo", () => ({ ComputeDemo: () => null }));

function summary(overrides: Partial<OperationsSummary> = {}): OperationsSummary {
  return {
    generated_at: "2026-10-30T20:00:00Z",
    rabbitmq: {
      status: "AVAILABLE",
      messages_ready: 5,
      messages_unacknowledged: 2,
      consumer_count: 4,
      message: null,
    },
    workers: { status: "AVAILABLE", items: [], message: null },
    postgresql: {
      status: "AVAILABLE",
      job_counts: { ACCEPTED: 1, QUEUED: 2, RUNNING: 1, SUCCEEDED: 8, FAILED: 0 },
      message: null,
    },
    visible_jobs_status: "AVAILABLE",
    visible_jobs: [],
    ...overrides,
  };
}

function worker(index: number, activity: "IDLE" | "BUSY" = "IDLE") {
  return {
    worker_key: `worker-safe-${index}`,
    label: `Worker ${index}`,
    availability: "AVAILABLE" as const,
    activity,
  };
}

beforeEach(() => {
  vi.mocked(getOperationsSummary).mockReset().mockResolvedValue(summary());
  vi.mocked(getJobFlow).mockReset().mockImplementation((_token, signal) => new Promise((_resolve, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  }));
});

afterEach(() => {
  cleanup();
  if (vi.isFakeTimers()) {
    vi.clearAllTimers();
    vi.useRealTimers();
  }
});

describe("animated operations console", () => {
  it("does not let a delayed monitor response roll back newer flow", async () => {
    vi.useFakeTimers();
    let resolve!: (value: OperationsSummary) => void;
    vi.mocked(getOperationsSummary).mockImplementation(() => new Promise((done) => { resolve = done; }));
    const job = { job_id: jobId, job_type: "bqm" as const, state: "RUNNING" as const,
      attempt_state: "RUNNING" as const, worker_key: "worker-safe-1" };
    vi.mocked(getJobFlow).mockResolvedValue(summary({ visible_jobs: [job] }));
    const view = render(<OperationsConsole enabled token={null} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => {
      resolve(summary({ workers: { status: "AVAILABLE", items: [worker(1)], message: null },
        visible_jobs: [{ ...job, state: "QUEUED" }] }));
    });
    expect(document.querySelector('[data-job-state="RUNNING"]')).not.toBeNull();
    expect(document.querySelector('[data-job-state="QUEUED"]')).toBeNull();
    view.unmount();
    expect(vi.mocked(getJobFlow).mock.calls[0][1]?.aborted).toBe(true);
  });
  it("updates flow at 500ms without repeating expensive monitoring", async () => {
    vi.useFakeTimers();
    const queued = { job_id: jobId, job_type: "bqm" as const, state: "QUEUED" as const,
      attempt_state: null, worker_key: null };
    vi.mocked(getJobFlow).mockResolvedValueOnce(summary({ visible_jobs: [queued] }))
      .mockResolvedValue(summary({ visible_jobs: [{ ...queued, state: "SUCCEEDED" }] }));
    render(<OperationsConsole enabled token={null} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(document.querySelector('[data-job-state="QUEUED"]')).not.toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(screen.getByRole("region", { name: "Recently completed jobs" })).toBeInTheDocument();
    expect(vi.mocked(getOperationsSummary)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getJobFlow)).toHaveBeenCalledTimes(2);
  });
  it("renders actual queue depth and a confirmed visitor queue block", async () => {
    vi.mocked(getOperationsSummary).mockResolvedValue(summary({
      visible_jobs: [{
        job_id: jobId, job_type: "monte_carlo_pi", state: "QUEUED",
        attempt_state: null, worker_key: null,
      }],
    }));
    render(<OperationsConsole enabled token={null} />);

    const queue = await screen.findByRole("region", { name: "Queue lane" });
    expect(within(queue).getByText("5")).toBeInTheDocument();
    expect(within(queue).getByText("2")).toBeInTheDocument();
    expect(within(queue).getByText("MC")).toBeInTheDocument();
    expect(within(queue).getByText("#a2cc")).toBeInTheDocument();
  });

  it.each([1, 4])("renders exactly %i discovered worker cards", async (count) => {
    vi.mocked(getOperationsSummary).mockResolvedValue(summary({
      workers: {
        status: "AVAILABLE",
        items: Array.from({ length: count }, (_, index) => worker(index + 1)),
        message: null,
      },
    }));
    render(<OperationsConsole enabled token="developer-token" />);

    const pool = await screen.findByRole("region", { name: "Discovered workers" });
    expect(within(pool).getAllByText(/^Worker \d$/)).toHaveLength(count);
    expect(within(pool).getByText(`${count} discovered`)).toBeInTheDocument();
  });

  it("updates the worker row when the discovered count changes", async () => {
    vi.useFakeTimers();
    vi.mocked(getOperationsSummary)
      .mockResolvedValueOnce(summary({
        workers: { status: "AVAILABLE", items: [worker(1)], message: null },
      }))
      .mockResolvedValue(summary({
        workers: {
          status: "AVAILABLE",
          items: [worker(1), worker(2), worker(3), worker(4)],
          message: null,
        },
      }));
    render(<OperationsConsole enabled token={null} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("1 discovered")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    expect(screen.getByText("4 discovered")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Discovered workers" }))
      .getAllByText(/^Worker \d$/)).toHaveLength(4);
  });

  it("moves a confirmed job from queue to its persisted worker assignment", async () => {
    vi.useFakeTimers();
    vi.mocked(getOperationsSummary)
      .mockResolvedValueOnce(summary({
        workers: { status: "AVAILABLE", items: [worker(1)], message: null },
        visible_jobs: [{ job_id: jobId, job_type: "monte_carlo_pi", state: "QUEUED", attempt_state: null, worker_key: null }],
      }))
      .mockResolvedValue(summary({
        workers: { status: "AVAILABLE", items: [worker(1, "BUSY")], message: null },
        visible_jobs: [{ job_id: jobId, job_type: "monte_carlo_pi", state: "RUNNING", attempt_state: "RUNNING", worker_key: "worker-safe-1" }],
      }));
    render(<OperationsConsole enabled token={null} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("region", { name: "Queue lane" })).toHaveTextContent("#a2cc");

    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    const pool = screen.getByRole("region", { name: "Discovered workers" });
    expect(within(pool).getByText("#a2cc")).toBeInTheDocument();
    expect(within(pool).getByText("RUNNING")).toBeInTheDocument();
  });

  it("shows an unassigned running lane when worker correlation is missing", async () => {
    vi.mocked(getOperationsSummary).mockResolvedValue(summary({
      workers: { status: "AVAILABLE", items: [worker(1, "BUSY")], message: null },
      visible_jobs: [{ job_id: jobId, job_type: "bqm", state: "RUNNING", attempt_state: "RUNNING", worker_key: null }],
    }));
    render(<OperationsConsole enabled token={null} />);

    expect(await screen.findByRole("region", { name: "Unassigned running jobs" }))
      .toHaveTextContent("worker correlation unavailable");
  });

  it("preserves retry timing and reassigns only after QUEUED and RUNNING observations", async () => {
    vi.useFakeTimers();
    const running = summary({
      workers: { status: "AVAILABLE", items: [worker(1, "BUSY"), worker(2)], message: null },
      visible_jobs: [{ job_id: jobId, job_type: "cqm", state: "RUNNING", attempt_state: "RUNNING", worker_key: "worker-safe-1" }],
    });
    const retry = summary({
      workers: { status: "AVAILABLE", items: [worker(1), worker(2)], message: null },
      visible_jobs: [{ job_id: jobId, job_type: "cqm", state: "RETRY_WAIT", attempt_state: "ABANDONED", worker_key: null }],
    });
    const queued = summary({
      workers: retry.workers,
      visible_jobs: [{ job_id: jobId, job_type: "cqm", state: "QUEUED", attempt_state: "ABANDONED", worker_key: null }],
    });
    const reassigned = summary({
      workers: { status: "AVAILABLE", items: [worker(1), worker(2, "BUSY")], message: null },
      visible_jobs: [{ job_id: jobId, job_type: "cqm", state: "RUNNING", attempt_state: "RUNNING", worker_key: "worker-safe-2" }],
    });
    vi.mocked(getOperationsSummary)
      .mockResolvedValueOnce(running).mockResolvedValueOnce(retry)
      .mockResolvedValueOnce(queued).mockResolvedValue(reassigned);
    render(<OperationsConsole enabled token={null} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("Worker 1")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(screen.getByRole("region", { name: "Jobs waiting to retry" })).toHaveTextContent("INTERRUPTED");
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(screen.getByRole("region", { name: "Queue lane" })).toHaveTextContent("#a2cc");
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(within(screen.getByRole("region", { name: "Discovered workers" })).getByText("Worker 2").closest("article"))
      .toHaveTextContent("#a2cc");
  });

  it("observes RUNNING to SUCCEEDED and notifies the Recent Jobs ledger", async () => {
    vi.useFakeTimers();
    const completed = vi.fn();
    vi.mocked(getOperationsSummary)
      .mockResolvedValueOnce(summary({
        workers: { status: "AVAILABLE", items: [worker(1, "BUSY")], message: null },
        visible_jobs: [{ job_id: jobId, job_type: "monte_carlo_pi", state: "RUNNING", attempt_state: "RUNNING", worker_key: "worker-safe-1" }],
      }))
      .mockResolvedValue(summary({
        workers: { status: "AVAILABLE", items: [worker(1)], message: null },
        visible_jobs: [{ job_id: jobId, job_type: "monte_carlo_pi", state: "SUCCEEDED", attempt_state: "SUCCEEDED", worker_key: null }],
      }));
    render(<OperationsConsole enabled token={null} onJobCompleted={completed} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("Worker 1")).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    expect(screen.getByRole("region", { name: "Recently completed jobs" })).toHaveTextContent("SUCCEEDED");
    expect(completed).toHaveBeenCalledWith(jobId);
  });

  it("keeps the last snapshot and marks it stale after a refresh failure", async () => {
    vi.useFakeTimers();
    vi.mocked(getOperationsSummary)
      .mockResolvedValueOnce(summary({ workers: { status: "AVAILABLE", items: [worker(1)], message: null } }))
      .mockRejectedValue(new Error("inspection timeout"));
    render(<OperationsConsole enabled token={null} />);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("Worker 1")).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));

    expect(screen.getByText("STALE")).toBeInTheDocument();
    expect(screen.getByText("Worker 1")).toBeInTheDocument();
    expect(screen.getByText(/last successful snapshot/)).toBeInTheDocument();
  });

  it("does not overlap requests and stops polling after unmount", async () => {
    vi.useFakeTimers();
    let resolveRequest: ((value: OperationsSummary) => void) | undefined;
    vi.mocked(getOperationsSummary).mockImplementation(() => new Promise((resolve) => {
      resolveRequest = resolve;
    }));
    const view = render(<OperationsConsole enabled token={null} />);
    expect(getOperationsSummary).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(9_000));
    expect(getOperationsSummary).toHaveBeenCalledTimes(1);
    await act(async () => resolveRequest?.(summary()));
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(9_000));
    expect(getOperationsSummary).toHaveBeenCalledTimes(1);
  });

  it("shows unavailable dependencies without converting them to zero", async () => {
    vi.mocked(getOperationsSummary).mockResolvedValue(summary({
      rabbitmq: { status: "UNAVAILABLE", messages_ready: null, messages_unacknowledged: null, consumer_count: null, message: "RabbitMQ unavailable" },
      postgresql: { status: "UNAVAILABLE", job_counts: null, message: "PostgreSQL unavailable" },
    }));
    render(<OperationsConsole enabled token={null} />);

    expect(await screen.findAllByText("Unavailable")).not.toHaveLength(0);
    expect(screen.getByText("PostgreSQL unavailable")).toBeInTheDocument();
  });

  it("renders anonymous-safe worker labels without internal identifiers", async () => {
    vi.mocked(getOperationsSummary).mockResolvedValue(summary({
      workers: { status: "AVAILABLE", items: [worker(31, "BUSY")], available: 1, idle: 0, busy: 1, message: null },
      visible_jobs: [{ job_id: jobId, job_type: "monte_carlo_pi", state: "RUNNING", attempt_state: "RUNNING", worker_key: "worker-safe-31" }],
    }));
    render(<OperationsConsole enabled token={null} />);

    expect(await screen.findByText("Worker 31")).toBeInTheDocument();
    expect(screen.queryByText(/celery@|computegrid-worker-/)).not.toBeInTheDocument();
    expect(getOperationsSummary).toHaveBeenCalledWith(null, expect.any(AbortSignal));
  });
});
