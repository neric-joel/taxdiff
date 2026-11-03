import { SOURCE_LABELS, capFirst } from "./format";
import type { NodeEvent, NodeId, NodeState, Source } from "./types";

export type IconName = "message" | "reader" | "file" | "check" | "calc" | "scale" | "book" | "split" | "hand" | "note" | "answer" | "list";

export interface NodeMeta { id: NodeId; name: string; service: string; icon: IconName }

// In the order the pipeline runs. Recompute returns (Engine A) runs first because it can stop the change;
// Cross-check (Engine B) and Cite the law wait for it (queued) and then run in parallel. Reviewer approval
// comes after routing because it shows the routes and amounts that approval applies. The names are plain
// words for a reviewer; the engine names, times and Live/Cached/Fallback badges stay in the graph and the
// step drawers. Node ids are the API's and never change.
export const NODES: NodeMeta[] = [
  { id: "question", name: "Question", service: "Your question or change", icon: "message" },
  { id: "intake", name: "Read the question", service: "Code first, then an AI model (Claude) if needed", icon: "reader" },
  { id: "documents", name: "Read documents", service: "Employer statements", icon: "file" },
  { id: "validate", name: "Validate", service: "Every value needs a source", icon: "check" },
  { id: "engine_a", name: "Recompute returns", service: "TaxDiff's tax engine", icon: "calc" },
  { id: "engine_b", name: "Cross-check", service: "PolicyEngine-US, a second engine", icon: "scale" },
  { id: "law", name: "Cite the law", service: "irs.gov, eCFR, Federal Register", icon: "book" },
  { id: "route", name: "Route returns", service: "Next step for each return", icon: "split" },
  { id: "approve", name: "Reviewer approval", service: "A named person decides", icon: "hand" },
  { id: "notes", name: "Write notes", service: "Notes and client letters", icon: "note" },
  { id: "explain", name: "Answer", service: "In plain words, from the run", icon: "answer" },
  { id: "audit", name: "Audit log", service: "What happened, and who", icon: "list" },
];
export const NODE_IDS = NODES.map((n) => n.id);
export const META = Object.fromEntries(NODES.map((n) => [n.id, n])) as Record<NodeId, NodeMeta>;

/** The service line of a step's card. Read the question names the reader that actually ran once it has run
 * ("Saved change set", "Pattern reader", "Anthropic API (claude-haiku-5-5)"); every other step keeps its own. */
export function serviceOf(meta: NodeMeta, view: NodeView): string {
  if (meta.id === "intake" && view.provider && (view.state === "done" || view.state === "failed")) {
    return view.provider === "pattern reader" ? "Read by code, no model needed" : capFirst(view.provider);
  }
  return meta.service;
}

// Top-left positions in graph units. Every node is 176 wide and about 90 tall; columns sit 208 apart and rows
// 104 apart, so the whole graph is 800 units wide and fits a 1280 px screen at full size. The first column
// reads down: Question, Read the question, Read documents. Validate, the three parallel steps and Route
// returns run to the right; the decision row sits underneath, below a gap the edge from Route returns uses.
// The card width is set in globals.css (.lane).
export const POSITIONS: Record<NodeId, { x: number; y: number }> = {
  question: { x: 0, y: 0 },
  intake: { x: 0, y: 104 },
  documents: { x: 0, y: 208 },
  validate: { x: 208, y: 104 },
  engine_a: { x: 416, y: 0 },
  engine_b: { x: 416, y: 104 },
  law: { x: 416, y: 208 },
  route: { x: 624, y: 104 },
  approve: { x: 0, y: 344 },
  notes: { x: 208, y: 344 },
  explain: { x: 416, y: 344 },
  audit: { x: 624, y: 344 },
};

// turn: how far above its target an edge makes its horizontal run, for the one edge that crosses the graph.
export interface EdgeMeta { id: string; source: NodeId; target: NodeId; sourceHandle: "out" | "down"; targetHandle: "in" | "up"; turn?: number }

const e = (source: NodeId, target: NodeId, sourceHandle: "out" | "down" = "out", targetHandle: "in" | "up" = "in", turn?: number): EdgeMeta =>
  ({ id: `${source}-${target}`, source, target, sourceHandle, targetHandle, turn });

export const EDGES: EdgeMeta[] = [
  e("question", "intake", "down", "up"),
  e("intake", "validate"),
  e("intake", "documents", "down", "up"),
  e("documents", "validate"),
  e("validate", "engine_a"),
  e("validate", "engine_b"),
  e("validate", "law"),
  e("engine_a", "route"),
  e("engine_b", "route"),
  e("law", "route"),
  e("route", "approve", "down", "up", 22),
  e("approve", "notes"),
  e("notes", "explain"),
  e("explain", "audit"),
];

export interface NodeView {
  state: NodeState;
  summary: string;
  duration_ms?: number | null;
  source?: Source;
  provider?: string;
  started_at?: string | null;
  input?: unknown;
  output?: unknown;
  request?: NodeEvent["request"];
  error?: string | null;
  fallback?: string | null;
  decision?: "approved" | "turned_down";
}
export type GraphView = Record<NodeId, NodeView>;

export function idleView(): GraphView {
  return Object.fromEntries(NODE_IDS.map((id) => [id, { state: "idle", summary: "" }])) as unknown as GraphView;
}

/** The node's view after one event. A start keeps nothing of an earlier run of the same node. */
export function applyEvent(view: GraphView, event: NodeEvent): GraphView {
  const next: NodeView =
    event.state === "running" || event.state === "queued"
      ? { state: event.state, summary: event.summary, started_at: event.started_at }
      : {
          state: event.state,
          summary: event.summary,
          duration_ms: event.duration_ms ?? null,
          source: event.source ?? null,
          provider: event.provider,
          started_at: event.started_at,
          input: event.input,
          output: event.output,
          request: event.request ?? null,
          error: event.error ?? null,
          fallback: event.fallback ?? null,
          decision: event.decision,
        };
  return { ...view, [event.node]: next };
}

export type EdgeState = "idle" | "active" | "passed" | "skipped";

/** An edge animates while its target is waiting for or working on what the source handed over. */
export function edgeState(view: GraphView, edge: EdgeMeta): EdgeState {
  const source = view[edge.source].state;
  const target = view[edge.target].state;
  const finished = (s: NodeState) => s === "done" || s === "skipped" || s === "waiting" || s === "failed";
  if (edge.source === "intake" && edge.target === "validate" && view.documents.state === "done") return "skipped";
  if (edge.source === "intake" && edge.target === "documents" && view.documents.state === "skipped") return "skipped";
  if (edge.source === "documents" && edge.target === "validate" && view.documents.state === "skipped") return "skipped";
  if ((target === "running" || target === "queued") && finished(source)) return "active";
  if (source === "skipped" || target === "skipped") return target === "idle" ? "idle" : "skipped";
  if (source === "done" && (target === "done" || target === "waiting" || target === "failed")) return "passed";
  return "idle";
}

/** The step's state in one word; the source (live, cached, fallback) is the badge's job. */
export function stateWord(view: NodeView): string {
  if (view.state === "done" && view.decision === "turned_down") return "Turned down";
  return {
    idle: "Idle",
    queued: "Queued",
    running: "Running",
    done: "Done",
    failed: "Failed",
    skipped: "Skipped",
    waiting: "Awaiting decision",
  }[view.state];
}

const COUNT_ORDER = ["live", "cached", "fallback", "mock", "done", "failed", "skipped", "awaiting decision", "not run"];

/** How the steps of a finished run ended, for the collapsed strip: "12 steps: 9 live, 1 cached, 2 skipped". */
export function stepCounts(view: GraphView): string {
  const counts = new Map<string, number>();
  for (const node of NODES) {
    const step = view[node.id];
    const key = step.state === "done" ? (step.source ? (SOURCE_LABELS[step.source] ?? step.source).toLowerCase() : "done")
      : step.state === "failed" ? "failed"
      : step.state === "skipped" ? "skipped"
      : step.state === "waiting" ? "awaiting decision"
      : "not run";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const parts = [...counts.keys()].sort((a, b) => rank(a) - rank(b)).map((key) => `${counts.get(key)} ${key}`);
  return `${NODES.length} steps: ${parts.join(", ")}`;
}

function rank(key: string): number {
  const index = COUNT_ORDER.indexOf(key);
  return index < 0 ? COUNT_ORDER.length : index;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 100) return `${ms.toFixed(1)} ms`;
  if (ms < 10_000) return `${Math.round(ms).toLocaleString("en-US")} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}
