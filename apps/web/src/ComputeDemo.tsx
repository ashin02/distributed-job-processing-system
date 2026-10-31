import { useEffect, useRef, useState } from "react";
import { DemoRun, latestDemoRun, startDemoRun } from "./api";

export function ComputeDemo({ enabled, token, onJobsChanged }: {
  enabled: boolean; token: string | null; onJobsChanged?: () => void;
}) {
  const [run, setRun] = useState<DemoRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [remaining, setRemaining] = useState(0);
  const pending = useRef(false);
  const key = useRef<string | null>(null);
  const startController = useRef<AbortController | null>(null);
  const alive = useRef(false);
  const generation = useRef(0);
  const changed = useRef(onJobsChanged);
  changed.current = onJobsChanged;
  const previousProgress = useRef("");
  const active = useRef(false);

  function receive(next: DemoRun | null) {
    active.current = next !== null && !next.execution_complete;
    const signature = next ? JSON.stringify([next.id, next.submitted, next.counts]) : "";
    if (signature !== previousProgress.current) changed.current?.();
    previousProgress.current = signature;
    setRun(next);
    setRemaining(Math.ceil(next?.remaining_seconds ?? 0));
    setError(null);
    setReady(true);
  }

  useEffect(() => {
    alive.current = true;
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    setReady(false);
    setRun(null);
    key.current = null;
    async function poll() {
      if (!enabled || disposed) return;
      if (!pending.current) {
        const captured = generation.current;
        controller = new AbortController();
        try {
          const next = await latestDemoRun(token, controller.signal);
          if (!disposed && !pending.current && captured === generation.current) receive(next);
        } catch (failure) {
          if (!disposed) setError(failure instanceof Error ? failure.message : "Demo status unavailable");
        }
      }
      if (!disposed) timer = window.setTimeout(() => void poll(), active.current ? 1_000 : 3_000);
    }
    void poll();
    return () => {
      disposed = true;
      generation.current += 1;
      alive.current = false;
      window.clearTimeout(timer);
      controller?.abort();
      startController.current?.abort();
    };
  }, [enabled, token]);

  useEffect(() => {
    const timer = window.setInterval(() => setRemaining((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, []);

  async function start() {
    if (pending.current) return;
    pending.current = true;
    generation.current += 1;
    const captured = generation.current;
    setBusy(true);
    key.current ??= crypto.randomUUID();
    startController.current = new AbortController();
    try {
      const next = await startDemoRun(token, key.current, startController.current.signal);
      if (alive.current && captured === generation.current) { receive(next); key.current = null; }
    } catch (failure) {
      if (alive.current && captured === generation.current) {
        setError(failure instanceof Error ? failure.message : "Could not start demo");
      }
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  }

  return <section className="compute-demo" aria-label="Computational Load Demo">
    <div><strong>Computational Load Demo</strong>
      <p>Run real CPU-intensive jobs for 20 seconds and watch the queue and workers process them.</p>
    </div>
    <button className="button-primary" onClick={() => void start()}
      disabled={!enabled || !ready || busy || run?.feed_status === "FEEDING"}>
      Run Compute Demo
    </button>
    {run && <div role="status">
      {run.feed_status === "FEEDING"
        ? `Submitting · ${remaining}s remaining`
        : run.execution_complete ? "Run complete"
          : "Feed complete — processing remaining jobs"}
      <span> · {run.submitted} submitted · {run.counts.QUEUED} queued · {run.counts.RUNNING} running
        · {run.counts.SUCCEEDED + run.counts.FAILED + run.counts.CANCELLED} terminal</span>
      {run.reason && <p>{run.reason}</p>}
    </div>}
    {error && <p role="alert">{error}{run ? " — last known progress" : ""}</p>}
  </section>;
}
