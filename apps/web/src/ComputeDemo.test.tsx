import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { ComputeDemo } from "./ComputeDemo";
import { DemoRun, latestDemoRun, startDemoRun } from "./api";

vi.mock("./api", () => ({ latestDemoRun: vi.fn(), startDemoRun: vi.fn() }));
const run: DemoRun = {
  id: "run-1", started_at: "2026-10-30T00:00:00Z", ends_at: "2026-10-30T00:00:20Z",
  remaining_seconds: 20, feed_status: "FEEDING", submitted: 2, reason: null,
  execution_complete: false,
  counts: { ACCEPTED: 1, QUEUED: 1, RUNNING: 0, RETRY_WAIT: 0, SUCCEEDED: 0, FAILED: 0, CANCELLED: 0 },
};
beforeEach(() => {
  vi.mocked(latestDemoRun).mockReset().mockResolvedValue(null);
  vi.mocked(startDemoRun).mockReset().mockResolvedValue(run);
});

it("starts a server feed once and displays actual counts", async () => {
  render(<ComputeDemo enabled token={null} />);
  await act(async () => { await Promise.resolve(); });
  await userEvent.click(screen.getByRole("button", { name: "Run Compute Demo" }));
  expect(startDemoRun).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button")).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("2 submitted");
});

it("restores a run, counts down, distinguishes drain and cleans up polling", async () => {
  vi.useFakeTimers();
  vi.mocked(latestDemoRun).mockResolvedValueOnce(run).mockResolvedValue({
    ...run, feed_status: "COMPLETE", remaining_seconds: 0,
  });
  const view = render(<ComputeDemo enabled token="developer" />);
  await act(async () => { await Promise.resolve(); });
  await act(async () => vi.advanceTimersByTimeAsync(1000));
  expect(latestDemoRun).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status")).toHaveTextContent("Feed complete — processing remaining jobs");
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  expect(screen.getByRole("status")).toHaveTextContent("Feed complete — processing remaining jobs");
  view.unmount();
  const calls = vi.mocked(latestDemoRun).mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(9000));
  expect(latestDemoRun).toHaveBeenCalledTimes(calls);
});

it("shows completion and start errors without pretending a run started", async () => {
  vi.mocked(latestDemoRun).mockResolvedValue({ ...run, feed_status: "COMPLETE", execution_complete: true });
  vi.mocked(startDemoRun).mockRejectedValue(new Error("Cooldown active"));
  render(<ComputeDemo enabled token={null} />);
  expect(await screen.findByRole("status")).toHaveTextContent("Run complete");
  await userEvent.click(screen.getByRole("button"));
  expect(await screen.findByRole("alert")).toHaveTextContent("Cooldown active");
});

it("does not overlap slow progress requests and aborts on unmount", async () => {
  vi.useFakeTimers();
  vi.mocked(latestDemoRun).mockImplementation(() => new Promise(() => {}));
  const view = render(<ComputeDemo enabled token={null} />);
  await act(async () => vi.advanceTimersByTimeAsync(9000));
  expect(latestDemoRun).toHaveBeenCalledTimes(1);
  const signal = vi.mocked(latestDemoRun).mock.calls[0][1];
  view.unmount();
  expect(signal?.aborted).toBe(true);
});

it("retains known progress when the backend becomes unavailable", async () => {
  vi.useFakeTimers();
  vi.mocked(latestDemoRun).mockResolvedValueOnce(run).mockRejectedValue(new Error("Unavailable"));
  render(<ComputeDemo enabled token={null} />);
  await act(async () => { await Promise.resolve(); });
  await act(async () => vi.advanceTimersByTimeAsync(3000));
  expect(screen.getByRole("status")).toHaveTextContent("2 submitted");
  expect(screen.getByRole("alert")).toHaveTextContent("last known progress");
});
