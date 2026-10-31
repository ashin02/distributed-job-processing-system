// One request at a time; cancellation and a generation-local guard fence late replies.
export function poll<T>(load: (signal: AbortSignal) => Promise<T>, receive: (value: T) => void,
  fail: (error: unknown) => void, interval: number) {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout>;
  let controller: AbortController;
  async function tick() {
    const started = Date.now();
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);
    try {
      const value = await load(controller.signal);
      if (!stopped) receive(value);
    } catch (error) {
      if (!stopped) fail(error);
    } finally {
      clearTimeout(timeout);
      if (!stopped) timer = setTimeout(() => void tick(), Math.max(0, interval - (Date.now() - started)));
    }
  }
  void tick();
  return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
}
