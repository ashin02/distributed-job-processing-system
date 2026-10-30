import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  BQMParameters,
  CQMParameters,
  getJob,
  JobCreateRequest,
  JobDetail,
  JobListItem,
  JobState,
  JobType,
  listJobs,
  submitJob,
  WeightedMaxCutParameters,
} from "./api";

const PAGE_SIZE = 10;
const POLL_INTERVAL_MS = 5_000;
const ACTIVE_POLL_INTERVAL_MS = 1_000;
const TOKEN_STORAGE_KEY = "computegrid-development-token";
const ACTIVE_JOB_STATES = new Set<JobState>(["ACCEPTED", "QUEUED", "RUNNING", "RETRY_WAIT"]);
const DEFAULT_GRAPH = JSON.stringify(
  {
    nodes: ["a", "b", "c", "d"],
    edges: [
      { source: "a", target: "b", weight: 3 },
      { source: "a", target: "c", weight: 1 },
      { source: "b", target: "c", weight: 2 },
      { source: "b", target: "d", weight: 4 },
      { source: "c", target: "d", weight: 5 },
    ],
  },
  null,
  2,
);
const DEFAULT_BQM = JSON.stringify(
  {
    vartype: "BINARY",
    variables: ["x", "y", "z"],
    linear: { x: -1, y: -1, z: 0.5 },
    quadratic: [
      { source: "x", target: "y", bias: 2 },
      { source: "y", target: "z", bias: -1 },
    ],
    offset: 0,
  },
  null,
  2,
);
const DEFAULT_CQM = JSON.stringify(
  {
    variables: ["x", "y", "z"],
    objective: { sense: "maximize", linear: { x: 4, y: 3, z: 2 }, offset: 0 },
    constraints: [
      { name: "capacity", linear: { x: 2, y: 2, z: 1 }, sense: "<=", rhs: 3 },
      { name: "choose-something", linear: { x: 1, y: 1, z: 1 }, sense: ">=", rhs: 1 },
    ],
  },
  null,
  2,
);

interface JobDefinition {
  title: string;
  subtitle: string;
  represents: string;
  applications: string;
  execution: string;
  beyond: string;
}

const JOB_DEFINITIONS: Record<JobType, JobDefinition> = {
  monte_carlo_pi: {
    title: "Monte Carlo π",
    subtitle: "Numerical sampling",
    represents: "A seeded sampling problem that estimates π from the fraction of random points inside a circle.",
    applications: "Simulation, uncertainty analysis, risk estimation, and validating distributed compute pipelines.",
    execution: "One worker generates reproducible pseudo-random points and calculates the estimate directly.",
    beyond: "Independent samples can run in parallel, while variance-reduction or quasi-Monte Carlo methods may improve accuracy. This is not a quantum optimization model.",
  },
  weighted_max_cut: {
    title: "Weighted Max-Cut",
    subtitle: "Graph → QUBO → exact solve",
    represents: "A graph partition problem that maximizes the total edge weight crossing between two groups.",
    applications: "Network community detection, circuit layout, statistical physics, and clustering approximations.",
    execution: "ComputeGrid converts the graph to a binary QUBO/BQM and checks every partition, using symmetry to skip equivalent complements.",
    beyond: "Exact enumeration grows exponentially. Larger graphs often use classical approximation or local-search heuristics; quantum annealing and hybrid solvers are also possible approaches, without a general speed advantage guarantee.",
  },
  bqm: {
    title: "BQM",
    subtitle: "QUBO or Ising model",
    represents: "A quadratic objective over BINARY {0,1} or SPIN {-1,+1} variables, with linear biases and pairwise interactions.",
    applications: "Scheduling, feature selection, portfolio models, assignment problems, and Ising-system studies.",
    execution: "ComputeGrid evaluates every assignment and returns the one with the lowest model energy.",
    beyond: "Exact enumeration doubles with each variable. Classical heuristics, specialized exact solvers, quantum annealing, and hybrid solvers offer different tradeoffs; none is automatically best for every model.",
  },
  cqm: {
    title: "CQM",
    subtitle: "Restricted constrained model",
    represents: "A constrained model that optimizes an objective while requiring every selected assignment to satisfy explicit rules.",
    applications: "Resource allocation, knapsack and selection problems, production planning, and constrained scheduling.",
    execution: "ComputeGrid enumerates binary assignments, filters them through linear constraints, and selects the best feasible objective value.",
    beyond: "Enumeration scales exponentially. Larger linear models commonly use MILP or constraint-programming solvers; hybrid methods may help some formulations, while quantum annealing generally requires a suitable reformulation.",
  },
};

type TimelineStageState = "complete" | "active" | "pending" | "failed";

interface TimelineStage {
  label: string;
  timestamp: string | null;
  duration: string | null;
  state: TimelineStageState;
}

function formatTimestamp(value: string | null): string {
  if (value === null) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function formatDuration(start: string | null, end: string | null): string | null {
  if (start === null || end === null) return null;
  const milliseconds = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "An unexpected error occurred.";
}

function StatusBadge({ state }: { state: JobState }) {
  return <span className={`status status-${state.toLowerCase()}`}>{state}</span>;
}

function JsonBlock({ value, empty }: { value: unknown; empty: string }) {
  if (value === null || value === undefined) return <p className="muted compact">{empty}</p>;
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

function isWeightedGraph(value: unknown): value is WeightedMaxCutParameters {
  if (typeof value !== "object" || value === null) return false;
  const graph = value as Partial<WeightedMaxCutParameters>;
  return Array.isArray(graph.nodes) && Array.isArray(graph.edges);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBQM(value: unknown): value is BQMParameters {
  if (!isRecord(value)) return false;
  return (
    (value.vartype === "BINARY" || value.vartype === "SPIN") &&
    Array.isArray(value.variables) &&
    isRecord(value.linear) &&
    Array.isArray(value.quadratic) &&
    typeof value.offset === "number"
  );
}

function isCQM(value: unknown): value is CQMParameters {
  if (!isRecord(value)) return false;
  return Array.isArray(value.variables) && isRecord(value.objective) && Array.isArray(value.constraints);
}

function displayJobType(type: JobType): string {
  return JOB_DEFINITIONS[type].title;
}

function JobInformation({ type }: { type: JobType }) {
  const definition = JOB_DEFINITIONS[type];
  return (
    <dl className="job-information">
      <div><dt>What it represents</dt><dd>{definition.represents}</dd></div>
      <div><dt>Example applications</dt><dd>{definition.applications}</dd></div>
      <div><dt>How this demo solves it</dt><dd>{definition.execution}</dd></div>
      <div><dt>Beyond this demo</dt><dd>{definition.beyond}</dd></div>
    </dl>
  );
}

function JsonEditor({
  label,
  value,
  onChange,
  hint,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint: string;
  disabled: boolean;
}) {
  return (
    <label>
      {label}
      <textarea
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        spellCheck={false}
      />
      <span>{hint}</span>
    </label>
  );
}

function GraphView({
  graph,
  partition,
}: {
  graph: WeightedMaxCutParameters;
  partition?: { side_a: string[]; side_b: string[] };
}) {
  const size = 280;
  const center = size / 2;
  const radius = 96;
  const positions = new Map(
    graph.nodes.map((node, index) => {
      const angle = (2 * Math.PI * index) / graph.nodes.length - Math.PI / 2;
      return [
        node,
        { x: center + radius * Math.cos(angle), y: center + radius * Math.sin(angle) },
      ] as const;
    }),
  );
  const sideB = new Set(partition?.side_b ?? []);

  return (
    <svg className="graph-view" viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Weighted graph">
      {graph.edges.map((edge) => {
        const source = positions.get(edge.source);
        const target = positions.get(edge.target);
        if (!source || !target) return null;
        const crossed = partition !== undefined && sideB.has(edge.source) !== sideB.has(edge.target);
        return (
          <g key={`${edge.source}-${edge.target}`} className={crossed ? "edge edge-cut" : "edge"}>
            <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} />
            <text x={(source.x + target.x) / 2} y={(source.y + target.y) / 2}>{edge.weight}</text>
          </g>
        );
      })}
      {graph.nodes.map((node) => {
        const position = positions.get(node)!;
        const side = partition ? (sideB.has(node) ? "b" : "a") : "plain";
        return (
          <g key={node} className={`graph-node graph-node-${side}`}>
            <circle cx={position.x} cy={position.y} r="21" />
            <text x={position.x} y={position.y}>{node}</text>
          </g>
        );
      })}
    </svg>
  );
}

function OptimizationResult({ job }: { job: JobDetail }) {
  if (!isWeightedGraph(job.parameters)) return null;
  const result = job.result;
  const rawPartition = result?.partition;
  const partition =
    typeof rawPartition === "object" && rawPartition !== null
      ? (rawPartition as { side_a: string[]; side_b: string[] })
      : undefined;
  const qubo = result?.qubo as
    | {
        linear?: Array<{ variable: string; bias: number }>;
        quadratic?: Array<{ source: string; target: string; bias: number }>;
        energy_relation?: string;
      }
    | undefined;
  const solver = result?.solver as
    | { name?: string; assignments_evaluated?: number; symmetry_breaking?: string }
    | undefined;

  return (
    <section className="optimization-result" aria-labelledby="optimization-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Classical optimization</p>
          <h3 id="optimization-heading">Graph and optimal cut</h3>
        </div>
        {typeof result?.cut_weight === "number" && (
          <div className="cut-score"><span>Optimal cut</span><strong>{result.cut_weight}</strong></div>
        )}
      </div>
      <div className="optimization-grid">
        <article className="graph-card">
          <GraphView graph={job.parameters} partition={partition} />
          {partition ? (
            <div className="partition-legend">
              <span><i className="side-a" />A · {partition.side_a.join(", ")}</span>
              <span><i className="side-b" />B · {partition.side_b.join(", ")}</span>
            </div>
          ) : <p className="muted compact">The partition appears after execution.</p>}
        </article>
        <article className="qubo-card">
          <div className="qubo-heading">
            <h3>QUBO coefficients</h3>
            <span>{qubo?.energy_relation ?? "Awaiting result"}</span>
          </div>
          {qubo?.linear && qubo.quadratic ? (
            <div className="coefficient-groups">
              <div><strong>Linear</strong>{qubo.linear.map((term) => (
                <code key={term.variable}>Q[{term.variable}] = {term.bias}</code>
              ))}</div>
              <div><strong>Quadratic</strong>{qubo.quadratic.map((term) => (
                <code key={`${term.source}-${term.target}`}>
                  Q[{term.source},{term.target}] = {term.bias}
                </code>
              ))}</div>
            </div>
          ) : <p className="muted compact">The QUBO model appears after execution.</p>}
        </article>
      </div>
      {solver && (
        <div className="solver-meta">
          <span><strong>Solver</strong>{solver.name ?? "—"}</span>
          <span><strong>Assignments</strong>{solver.assignments_evaluated ?? "—"}</span>
          <span><strong>Symmetry</strong>{solver.symmetry_breaking ?? "—"}</span>
        </div>
      )}
    </section>
  );
}

function GenericModelResult({ job }: { job: JobDetail }) {
  const result = job.result;
  if (!result || (job.type !== "bqm" && job.type !== "cqm")) return null;
  const assignment = isRecord(result.assignment) ? result.assignment : null;
  const solver = isRecord(result.solver) ? result.solver : null;
  const isCqm = job.type === "cqm";
  const infeasible = isCqm && result.status === "INFEASIBLE";
  const metric = isCqm ? result.objective_value : result.minimum_energy;
  const constraints = Array.isArray(result.constraint_evaluations)
    ? result.constraint_evaluations.filter(isRecord)
    : [];

  return (
    <section className="model-result" aria-labelledby="model-result-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Exact classical result</p>
          <h3 id="model-result-heading">{isCqm ? "Constrained solution" : "Minimum-energy sample"}</h3>
        </div>
        <span className={`solution-status ${infeasible ? "solution-infeasible" : "solution-optimal"}`}>
          {String(result.status ?? "OPTIMAL")}
        </span>
      </div>
      {infeasible ? (
        <div className="infeasible-message">
          <strong>No feasible assignment exists.</strong>
          <span>The worker completed successfully and exhaustively checked the bounded model.</span>
        </div>
      ) : (
        <div className="solution-grid">
          <div className="solution-metric">
            <span>{isCqm ? "Objective value" : "Minimum energy"}</span>
            <strong>{typeof metric === "number" ? metric : "—"}</strong>
            <small>
              {isCqm
                ? `${String(result.objective_sense ?? "minimize")} objective`
                : `${String(result.vartype ?? "BINARY")} variables`}
            </small>
          </div>
          <div className="assignment-grid">
            {assignment && Object.entries(assignment).map(([variable, value]) => (
              <span key={variable}><code>{variable}</code><strong>{String(value)}</strong></span>
            ))}
          </div>
        </div>
      )}
      {constraints.length > 0 && (
        <div className="constraint-list">
          {constraints.map((constraint) => (
            <span key={String(constraint.name)}>
              <strong>{String(constraint.name)}</strong>
              <code>{String(constraint.lhs)} {String(constraint.sense)} {String(constraint.rhs)}</code>
              <i>✓ satisfied</i>
            </span>
          ))}
        </div>
      )}
      {solver && (
        <div className="solver-meta">
          <span><strong>Solver</strong>{String(solver.name ?? "—")}</span>
          <span><strong>Assignments</strong>{String(solver.assignments_evaluated ?? "—")}</span>
          <span><strong>Feasible</strong>{String(solver.feasible_assignments ?? "n/a")}</span>
        </div>
      )}
    </section>
  );
}

function ExecutionTimeline({ job }: { job: JobDetail }) {
  const terminal = ["SUCCEEDED", "FAILED", "CANCELLED"].includes(job.state);
  const currentIndex =
    job.state === "ACCEPTED" ? 0 : job.state === "QUEUED" ? 1 : terminal ? 3 : 2;
  const terminalLabel = terminal ? job.state : "SUCCEEDED / FAILED";
  const timestamps = [job.created_at, job.queued_at, job.started_at, job.completed_at];
  const durations = [
    null,
    formatDuration(job.queued_at, job.started_at),
    formatDuration(job.started_at, job.completed_at),
    formatDuration(job.created_at, job.completed_at),
  ];
  const labels = ["ACCEPTED", "QUEUED", "RUNNING", terminalLabel];
  const stages: TimelineStage[] = labels.map((label, index) => {
    let state: TimelineStageState = "pending";
    if (index < currentIndex) state = "complete";
    if (index === currentIndex) state = terminal && job.state === "FAILED" ? "failed" : "active";
    if (terminal && index === currentIndex && job.state !== "FAILED") state = "complete";
    return { label, timestamp: timestamps[index] ?? null, duration: durations[index] ?? null, state };
  });

  return (
    <section className="execution-timeline" aria-labelledby="execution-timeline-heading">
      <div className="timeline-heading">
        <div>
          <p className="eyebrow">Lifecycle</p>
          <h3 id="execution-timeline-heading">Execution timeline</h3>
        </div>
        {ACTIVE_JOB_STATES.has(job.state) && (
          <span className="live-refresh"><span aria-hidden="true" />Live · refreshes every 1s</span>
        )}
      </div>
      <ol aria-label="Execution timeline" className="timeline-stages">
        {stages.map((stage) => (
          <li
            className={`timeline-stage timeline-stage-${stage.state}`}
            data-stage-state={stage.state}
            key={stage.label}
          >
            <span className="timeline-marker" aria-hidden="true">
              {stage.state === "complete" ? "✓" : stage.state === "failed" ? "!" : ""}
            </span>
            <div className="timeline-stage-copy">
              <strong>{stage.label}</strong>
              <time dateTime={stage.timestamp ?? undefined}>
                {stage.timestamp === null ? "Awaiting transition" : formatTimestamp(stage.timestamp)}
              </time>
              {stage.duration !== null && (
                <span>{stage.label === terminalLabel ? "Total" : "Duration"} · {stage.duration}</span>
              )}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

interface AppProps {
  demoMode?: boolean;
}

export default function App({
  demoMode = import.meta.env.VITE_DEMO_MODE === "true",
}: AppProps) {
  const storedToken = sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  const [tokenDraft, setTokenDraft] = useState(storedToken);
  const [token, setToken] = useState(storedToken);
  const [jobType, setJobType] = useState<JobType>("monte_carlo_pi");
  const [samples, setSamples] = useState("1000000");
  const [seed, setSeed] = useState("42");
  const [graphJson, setGraphJson] = useState(DEFAULT_GRAPH);
  const [bqmJson, setBqmJson] = useState(DEFAULT_BQM);
  const [cqmJson, setCqmJson] = useState(DEFAULT_CQM);
  const [expandedInfo, setExpandedInfo] = useState<JobType | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  const connected = demoMode || token.length > 0;
  const authToken = demoMode ? null : token || null;
  const monteCarloSampleLimit = demoMode ? 1_000_000 : 10_000_000;
  const modelVariableLimit = demoMode ? 12 : 16;
  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selectedSummary = useMemo(
    () => jobs.find((job) => job.job_id === selectedJobId) ?? null,
    [jobs, selectedJobId],
  );
  const selectedState =
    detail?.job_id === selectedJobId ? detail.state : selectedSummary?.state;
  const selectedJobIsActive = selectedState !== undefined && ACTIVE_JOB_STATES.has(selectedState);

  const loadJobs = useCallback(
    async (targetOffset = offset, quiet = false) => {
      if (!connected) return;
      if (!quiet) setListLoading(true);
      setListError(null);
      try {
        const page = await listJobs(authToken, PAGE_SIZE, targetOffset);
        setJobs(page.items);
        setTotal(page.total);
      } catch (error) {
        setListError(errorMessage(error));
      } finally {
        if (!quiet) setListLoading(false);
      }
    },
    [authToken, connected, offset],
  );

  const loadDetail = useCallback(
    async (jobId: string, quiet = false) => {
      if (!connected) return;
      if (!quiet) setDetailLoading(true);
      setDetailError(null);
      try {
        setDetail(await getJob(authToken, jobId));
      } catch (error) {
        setDetailError(errorMessage(error));
      } finally {
        if (!quiet) setDetailLoading(false);
      }
    },
    [authToken, connected],
  );

  useEffect(() => {
    if (connected) void loadJobs();
    else {
      setJobs([]);
      setTotal(0);
      setDetail(null);
    }
  }, [connected, loadJobs]);

  useEffect(() => {
    if (selectedJobId && connected) void loadDetail(selectedJobId);
  }, [connected, loadDetail, selectedJobId]);

  useEffect(() => {
    if (!polling || !connected || selectedJobIsActive) return undefined;
    const timer = window.setInterval(() => {
      void loadJobs(offset, true);
      if (selectedJobId) void loadDetail(selectedJobId, true);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [connected, loadDetail, loadJobs, offset, polling, selectedJobId, selectedJobIsActive]);

  useEffect(() => {
    if (!connected || !selectedJobId || !selectedJobIsActive) return undefined;
    const timer = window.setInterval(() => {
      void loadDetail(selectedJobId, true);
      void loadJobs(offset, true);
    }, ACTIVE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [connected, loadDetail, loadJobs, offset, selectedJobId, selectedJobIsActive]);

  function connect(event: FormEvent) {
    event.preventDefault();
    const nextToken = tokenDraft.trim();
    if (!nextToken) return;
    sessionStorage.setItem(TOKEN_STORAGE_KEY, nextToken);
    setToken(nextToken);
    setOffset(0);
    setNotice("Development token connected for this browser session.");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    let request: JobCreateRequest;
    if (jobType === "monte_carlo_pi") {
      const sampleCount = Number(samples);
      const seedValue = Number(seed);
      if (
        !Number.isInteger(sampleCount) ||
        sampleCount < 1 ||
        sampleCount > monteCarloSampleLimit
      ) {
        setSubmitError(
          `Samples must be an integer between 1 and ${monteCarloSampleLimit.toLocaleString()}.`,
        );
        return;
      }
      if (!Number.isInteger(seedValue)) {
        setSubmitError("Seed must be an integer.");
        return;
      }
      request = {
        type: "monte_carlo_pi",
        parameters: { samples: sampleCount, seed: seedValue },
      };
    } else if (jobType === "weighted_max_cut") {
      try {
        const graph = JSON.parse(graphJson) as unknown;
        if (!isWeightedGraph(graph)) throw new Error("Graph must contain nodes and edges arrays.");
        request = { type: "weighted_max_cut", parameters: graph };
      } catch (error) {
        setSubmitError(errorMessage(error));
        return;
      }
    } else if (jobType === "bqm") {
      try {
        const model = JSON.parse(bqmJson) as unknown;
        if (!isBQM(model)) {
          throw new Error("BQM must define vartype, variables, linear, quadratic, and offset.");
        }
        request = { type: "bqm", parameters: model };
      } catch (error) {
        setSubmitError(errorMessage(error));
        return;
      }
    } else {
      try {
        const model = JSON.parse(cqmJson) as unknown;
        if (!isCQM(model)) {
          throw new Error("CQM must define variables, objective, and constraints.");
        }
        request = { type: "cqm", parameters: model };
      } catch (error) {
        setSubmitError(errorMessage(error));
        return;
      }
    }

    setSubmitting(true);
    setSubmitError(null);
    setNotice(null);
    try {
      const accepted = await submitJob(
        authToken,
        request,
        idempotencyKey,
      );
      setNotice(`Job ${accepted.job_id} was durably accepted for asynchronous execution.`);
      setIdempotencyKey(crypto.randomUUID());
      setOffset(0);
      setSelectedJobId(accepted.job_id);
      await loadJobs(0);
    } catch (error) {
      setSubmitError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">CG</span>
          <div>
            <p className="eyebrow">Local operations console</p>
            <h1>ComputeGrid</h1>
          </div>
        </div>
        {demoMode ? (
          <div className="auth-form demo-access" aria-label="Access mode">
            <strong>Anonymous demo</strong>
            <span className="connection connected">Private browser session</span>
          </div>
        ) : <form className="auth-form" onSubmit={connect}>
          <label htmlFor="api-token">Development API token</label>
          <div className="inline-control">
            <input
              id="api-token"
              type="password"
              value={tokenDraft}
              onChange={(event) => setTokenDraft(event.target.value)}
              placeholder="Paste local token"
              autoComplete="off"
            />
            <button type="submit" className="button-secondary" disabled={!tokenDraft.trim()}>
              {connected ? "Reconnect" : "Connect"}
            </button>
          </div>
          <span className={connected ? "connection connected" : "connection"}>
            {connected ? "Connected" : "Token required"}
          </span>
        </form>}
      </header>

      <main>
        <section className="intro panel">
          <div>
            <p className="eyebrow">Distributed execution</p>
            <h2>Reliable execution for compute and optimization models.</h2>
            <p className="muted">
              Submit Monte Carlo, graph optimization, BQM, and restricted CQM jobs. Every
              transition, retry, lease recovery, and durable result stays visible.
            </p>
          </div>
          <div className="metric">
            <span>Visible jobs</span>
            <strong>{connected ? total : "—"}</strong>
          </div>
        </section>

        {notice && <div className="notice success" role="status">{notice}</div>}

        <div className="workspace-grid">
          <section className="panel submit-panel">
            <div className="section-heading">
              <div>
                <p className="eyebrow">New job</p>
                <h2>{displayJobType(jobType)}</h2>
              </div>
              <span className="execution-label">Async worker</span>
            </div>
            <form onSubmit={submit} className="stacked-form">
              <div className="job-type-switch" aria-label="Job type">
                {(Object.entries(JOB_DEFINITIONS) as Array<[JobType, JobDefinition]>).map(
                  ([type, definition]) => (
                    <div
                      className={`job-type-card ${jobType === type ? "job-type-card-selected" : ""}`}
                      key={type}
                    >
                      <button
                        type="button"
                        className="job-type-select"
                        aria-pressed={jobType === type}
                        onClick={() => setJobType(type)}
                      >
                        {definition.title}<span>{definition.subtitle}</span>
                      </button>
                      <button
                        type="button"
                        className="job-info-button"
                        aria-label={`About ${definition.title}`}
                        aria-expanded={expandedInfo === type}
                        aria-controls="job-information"
                        onClick={() => setExpandedInfo(expandedInfo === type ? null : type)}
                      >i</button>
                    </div>
                  ),
                )}
              </div>
              <p className="selection-disclosure">
                All jobs run locally on classical CPU workers—no QPU or remote solver is used.
              </p>
              {expandedInfo && (
                <div id="job-information" className="job-information-panel">
                  <div><strong>{displayJobType(expandedInfo)}</strong></div>
                  <JobInformation type={expandedInfo} />
                </div>
              )}
              {jobType === "monte_carlo_pi" ? (
                <>
                  <label>
                    Samples
                    <input
                      type="number"
                      min="1"
                      max={monteCarloSampleLimit}
                      step="1"
                      value={samples}
                      onChange={(event) => setSamples(event.target.value)}
                      disabled={!connected || submitting}
                    />
                    <span>1 to {monteCarloSampleLimit.toLocaleString()} deterministic samples</span>
                  </label>
                  <label>
                    Seed
                    <input
                      type="number"
                      step="1"
                      value={seed}
                      onChange={(event) => setSeed(event.target.value)}
                      disabled={!connected || submitting}
                    />
                    <span>Integer used to reproduce the workload</span>
                  </label>
                </>
              ) : jobType === "weighted_max_cut" ? (
                <JsonEditor
                  label="Weighted graph JSON"
                  value={graphJson}
                  onChange={setGraphJson}
                  hint={`2–${modelVariableLimit} nodes, unique undirected edges, positive weights`}
                  disabled={!connected || submitting}
                />
              ) : jobType === "bqm" ? (
                <JsonEditor
                  label="Binary Quadratic Model JSON"
                  value={bqmJson}
                  onChange={setBqmJson}
                  hint={`BINARY or SPIN model with at most ${modelVariableLimit} variables`}
                  disabled={!connected || submitting}
                />
              ) : (
                <JsonEditor
                  label="Constrained Quadratic Model JSON"
                  value={cqmJson}
                  onChange={setCqmJson}
                  hint={`Restricted binary/linear CQM with at most ${modelVariableLimit} variables and 32 constraints`}
                  disabled={!connected || submitting}
                />
              )}
              <div className="idempotency-note">
                <span>Idempotency key</span>
                <code title={idempotencyKey}>{idempotencyKey}</code>
              </div>
              {submitError && <div className="notice error" role="alert">{submitError}</div>}
              <button type="submit" className="button-primary" disabled={!connected || submitting}>
                {submitting ? "Submitting…" : "Submit job"}
              </button>
            </form>
          </section>

          <section className="panel jobs-panel">
            <div className="section-heading jobs-heading">
              <div>
                <p className="eyebrow">Job ledger</p>
                <h2>Recent jobs</h2>
              </div>
              <div className="toolbar">
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={polling}
                    onChange={(event) => setPolling(event.target.checked)}
                    disabled={!connected}
                  />
                  Poll every 5s
                </label>
                <button
                  type="button"
                  className="button-secondary"
                  onClick={() => void loadJobs()}
                  disabled={!connected || listLoading}
                >
                  Refresh
                </button>
              </div>
            </div>

            {!connected && <div className="empty-state">Connect a development token to load jobs.</div>}
            {connected && listLoading && <div className="empty-state" role="status">Loading jobs…</div>}
            {connected && listError && <div className="notice error" role="alert">{listError}</div>}
            {connected && !listLoading && !listError && jobs.length === 0 && (
              <div className="empty-state">No jobs yet. Submit one to create the first record.</div>
            )}
            {jobs.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Job</th><th>Status</th><th>Created</th><th>Updated</th></tr>
                  </thead>
                  <tbody>
                    {jobs.map((job) => (
                      <tr key={job.job_id} className={job.job_id === selectedJobId ? "selected" : ""}>
                        <td>
                          <button className="job-link" onClick={() => setSelectedJobId(job.job_id)}>
                            <strong>{displayJobType(job.type)}</strong>
                            <span>{job.job_id.slice(0, 12)}…</span>
                          </button>
                        </td>
                        <td><StatusBadge state={job.state} /></td>
                        <td>{formatTimestamp(job.created_at)}</td>
                        <td>{formatTimestamp(job.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="pagination">
              <button
                type="button"
                className="button-secondary"
                disabled={offset === 0 || listLoading}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </button>
              <span>Page {pageNumber} of {pageCount}</span>
              <button
                type="button"
                className="button-secondary"
                disabled={offset + PAGE_SIZE >= total || listLoading}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </button>
            </div>
          </section>
        </div>

        <section className="panel detail-panel">
          <div className="section-heading jobs-heading">
            <div>
              <p className="eyebrow">Inspection</p>
              <h2>Job detail</h2>
            </div>
            <div className="toolbar">
              {(detail ?? selectedSummary) && <StatusBadge state={(detail ?? selectedSummary)!.state} />}
              <button
                type="button"
                className="button-secondary"
                onClick={() => selectedJobId && void loadDetail(selectedJobId)}
                disabled={!selectedJobId || detailLoading}
              >
                Refresh detail
              </button>
            </div>
          </div>
          {!selectedJobId && <div className="empty-state">Select a job from the ledger to inspect it.</div>}
          {selectedJobId && detailLoading && <div className="empty-state" role="status">Loading detail…</div>}
          {detailError && <div className="notice error" role="alert">{detailError}</div>}
          {detail && !detailLoading && (
            <div className="detail-content">
              <div className="detail-meta">
                <div><span>Job ID</span><code>{detail.job_id}</code></div>
                <div><span>Created</span><strong>{formatTimestamp(detail.created_at)}</strong></div>
                <div><span>Updated</span><strong>{formatTimestamp(detail.updated_at)}</strong></div>
                <div><span>Completed</span><strong>{formatTimestamp(detail.completed_at)}</strong></div>
              </div>
              <ExecutionTimeline job={detail} />
              {detail.type === "weighted_max_cut" && <OptimizationResult job={detail} />}
              {(detail.type === "bqm" || detail.type === "cqm") && (
                <GenericModelResult job={detail} />
              )}
              <div className={`detail-grid ${detail.type === "weighted_max_cut" ? "detail-grid-compact" : ""}`}>
                <article><h3>Parameters</h3><JsonBlock value={detail.parameters} empty="No parameters." /></article>
                <article><h3>Result</h3><JsonBlock value={detail.result} empty="No result has been committed." /></article>
                <article><h3>Error</h3><JsonBlock value={detail.error} empty="No error recorded." /></article>
              </div>
              <div className="attempts">
                <h3>Attempts</h3>
                {detail.attempts.length === 0 ? (
                  <p className="muted">No execution attempt has been recorded yet.</p>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>#</th><th>Status</th><th>Worker</th><th>Started</th><th>Lease deadline</th><th>Finished</th><th>Duration</th><th>Failure</th></tr></thead>
                      <tbody>
                        {detail.attempts.map((attempt) => (
                          <tr key={attempt.attempt_id}>
                            <td>{attempt.attempt_number}</td>
                            <td><span className={`attempt-state attempt-${attempt.state.toLowerCase()}`}>{attempt.state}</span></td>
                            <td>{attempt.worker_id ?? "—"}</td>
                            <td>{formatTimestamp(attempt.started_at)}</td>
                            <td>{formatTimestamp(attempt.lease_expires_at)}</td>
                            <td>{formatTimestamp(attempt.finished_at)}</td>
                            <td>{formatDuration(attempt.started_at, attempt.finished_at) ?? "—"}</td>
                            <td className="attempt-error">{attempt.error ? String(attempt.error.message ?? attempt.error.type ?? "Recorded") : "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
