import { afterEach, expect, it, vi } from "vitest";
import { poll } from "./poll";

afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

it("polls at 500ms and stops on cleanup", async () => {
  vi.useFakeTimers();
  const load = vi.fn().mockResolvedValue("QUEUED");
  const receive = vi.fn();
  const stop = poll(load, receive, vi.fn(), 500);
  await vi.advanceTimersByTimeAsync(499);
  expect(load).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(load).toHaveBeenCalledTimes(2);
  stop();
  await vi.advanceTimersByTimeAsync(2000);
  expect(load).toHaveBeenCalledTimes(2);
});

it("serializes slow requests and ignores replies after cleanup", async () => {
  vi.useFakeTimers();
  let resolve!: (value: string) => void;
  const load = vi.fn(() => new Promise<string>((done) => { resolve = done; }));
  const receive = vi.fn();
  const stop = poll(load, receive, vi.fn(), 500);
  await vi.advanceTimersByTimeAsync(1500);
  expect(load).toHaveBeenCalledTimes(1);
  stop();
  resolve("RUNNING");
  await vi.advanceTimersByTimeAsync(0);
  expect(receive).not.toHaveBeenCalled();
});

it("delivers rapid authoritative transitions in order", async () => {
  vi.useFakeTimers();
  const load = vi.fn().mockResolvedValueOnce("QUEUED")
    .mockResolvedValueOnce("RUNNING").mockResolvedValue("SUCCEEDED");
  const receive = vi.fn();
  const stop = poll(load, receive, vi.fn(), 500);
  await vi.advanceTimersByTimeAsync(1000);
  expect(receive.mock.calls.map(([state]) => state)).toEqual(["QUEUED", "RUNNING", "SUCCEEDED"]);
  stop();
});
