// Shapes of the API's events and results (src/taxdiff/api/pipeline.py and payloads.py).

export type NodeId =
  | "question" | "intake" | "documents" | "validate" | "engine_a" | "engine_b"
  | "law" | "route" | "approve" | "notes" | "explain" | "audit";

export type NodeState = "idle" | "queued" | "running" | "done" | "failed" | "skipped" | "waiting";
export type Source = "live" | "cached" | "mock" | "fallback" | null;
export type RouteKey = "update" | "reviewer" | "amendment" | "document" | "below_materiality" | "no_change";

export interface Amount {
  value: string | null;
  text: string;
  unknown?: boolean;
  needs?: string[];
  range?: [string, string]; // while a document is outstanding: the least and the most the change can be
}

export interface LlmCall {
  provider?: string;
  model?: string;
  ms?: number;
  tokens_in?: number | null;
  tokens_out?: number | null;
  max_tokens?: number | null;
  outcome?: string;
}

export interface NodeEvent {
  type: "node";
  run_id: string;
  t: number;
  node: NodeId;
  state: Exclude<NodeState, "idle">;
  started_at: string | null;
  summary: string;
  duration_ms?: number | null;
  source?: Source;
  provider?: string;
  input?: unknown;
  output?: unknown;
  request?: { calls?: LlmCall[]; tokens_in?: number; tokens_out?: number; requests?: string[] } | null;
  error?: string | null;
  fallback?: string | null;
  decision?: "approved" | "turned_down";
}

export interface RunStarted { type: "run_started"; run_id: string; t: number; mode: "question" | "decision"; question: string; at: string; history: string[]; decision?: string }
export interface ResultEvent { type: "result"; run_id: string; t: number; result: RunResult }
export interface RunFinished { type: "run_finished"; run_id: string; t: number; status: string; total_ms: number }
export interface ErrorEvent { type: "error"; run_id: string; t: number; message: string }
export type StreamEvent = NodeEvent | RunStarted | ResultEvent | RunFinished | ErrorEvent;

export interface ChainRead {
  kind: "rule" | "fact" | "definition";
  name: string;
  label?: string;
  value?: string;
  cite?: string;
  url?: string | null;
  set_by?: { change: string; title: string; cite: string } | null;
  source?: { change?: string; title?: string; document?: string; field?: string; text?: string };
  summary?: string;
  version?: string;
  touched: boolean;
}

export interface ChainNode { line: string; label: string; no: string; value: Amount; reads: ChainRead[]; uses: ChainNode[] }

// One line where the two engines differ, as the API words it. difference ("$50") and no ("1-A 21") are optional:
// the screen never subtracts the two amounts itself.
export interface ParityDiff { line: string; label: string; engine_a: string; policyengine: string; difference?: string; no?: string }
// reason is filled when the engines differ ("Schedule 1-A line 19 counts whole $1,000 steps; ..."); authority
// names the source that settles which reading is right.
export interface Parity { state: "agree" | "differs" | "not_checked"; reason: string; diffs: ParityDiff[]; authority?: string }

export interface ImpactLine {
  line: string; label: string; no: string; before: Amount; after: Amount; delta: Amount | null;
  citation: { text: string; url: string | null; by: string }; chain: ChainNode;
}

// One step of the Schedule 1-A part a change moved, as the API words it. ref is the form line ("16"), or ""
// for a step with no form line; value comes formatted ("$151,500").
export interface WorksheetStep { line: string; ref: string; label: string; value: string }
export interface Worksheet { before: WorksheetStep[]; after: WorksheetStep[] }
// A filed return's corrected-return preview, in Form 1040-X columns: A as filed, B the change, C corrected.
export interface AmendedLine { line: string; label: string; as_filed: string; change: string; corrected: string }

/** How a return's changed line is computed: the method in words before and after the change, the worksheet
 * steps, and for a filed return the corrected-return preview. Any of them can be missing. */
export interface Computation {
  method?: string | null;
  method_before?: string | null;
  worksheet?: Worksheet | null;
  amended?: AmendedLine[] | null;
}

export interface ImpactRow extends Computation {
  client: string; status: string; filing: string; route: RouteKey; route_label: string;
  tax_before: Amount; tax_after: Amount; tax_change: Amount; vs_filed: Amount | null;
  // What the return waits for from the client. Any row with a need is a document request, whatever its route.
  parity: Parity | null; needs: string[]; lines: ImpactLine[]; touched: string[];
  primary_line?: string; // the line key the row leads with
  route_reason?: string; // why the return got its route, in a few words
  conflict_change?: Amount | null; // a conflict priced under the Notice's rule, against the return as filed or prepared
  conflict?: string | null; // a document that contradicts the return as filed
}

// Gross moves, so a refund on one return never hides more tax on others: the returns and the sum, by direction.
export interface Move { returns: number; amount: Amount }
export interface Moves { more: Move; less: Move }

/** Returns whose tax was unknown and is now known (C-017 after its statement), with the change against the
 * return as filed. */
export interface Settled { returns: number; clients: string[]; vs_filed: Amount | null }
/** A reached return where PolicyEngine-US and this project's engine differ, and why. */
export interface Differs { client: string; line: string; label: string; engine_a: string; policyengine: string; reason: string; difference?: string; no?: string }

export interface Kpis {
  reached: number; not_reached: number; unexpected: number; total: number;
  tax_change: Amount | null; tax_change_unknown: number; vs_filed: Amount | null;
  moves?: Moves; filed_moves?: Moves;
  unknown_moves?: { returns: number; range: Amount | null };
  conflict_moves?: { returns: number; clients: string[]; amount: Amount | null };
  settled?: Settled;
  differs?: Differs[];
  routes: Record<RouteKey, number>;
  parity: { agree: number; differs: number; not_checked: number };
  parity_reached: { agree: number; differs: number; not_checked: number };
}

export interface BookRow {
  client: string; status: "prep" | "review" | "filed"; filing: string; agi: Amount; sch1a_total: Amount;
  taxable_income: Amount; tax: Amount; as_filed_tax: Amount | null; facts_version: number;
  parity: "agree" | "differs" | "not_checked"; route: RouteKey | ""; case: string;
}

export interface NotReachedRow { client: string; status: string; reason: string; moved: boolean }
export interface Authority { key: string; title: string; url: string; scope: string; excerpt: string; as_of: string; fetched: boolean; request: string; fallback: string }
export interface AuditEvent {
  at: string; change_id: string; title: string; kind: string; author: string;
  // The API's words, shown as given: previewed, applied, turned down, failed validation, not recognized,
  // stopped, refused, error. Events this tab records itself say signed off, undone, session reset or
  // materiality changed, and carry local: true.
  outcome: string; detail: string; pack_version: number;
  approver: string; pack_sha: string; facts: string; materiality: string; parity: string; engine: string;
  client?: string; role?: string; decision?: string; local?: boolean;
  at_utc?: string; // the same moment in UTC, worked out in the browser, so the log shows the viewer's clock
}
export interface Link { client?: string; line?: string; label?: string; question?: string }
// A piece of an answer section: text, and the return row, line or example question it links to.
export interface AnswerPart { text: string; target: Link | null }
export type AnswerSection =
  | { type: "paragraph"; content: AnswerPart[] }
  | { type: "facts"; title: string | null; rows: { label: string; value: AnswerPart[] }[] }
  | { type: "list"; title: string | null; items: AnswerPart[][] };
// text is the whole answer as one paragraph; headline is its first sentence (the only part a model may
// reword), and sections are built by code.
export interface Answer {
  text: string; links: (Link | null)[]; source: string;
  headline?: string; headline_links?: (Link | null)[]; sections?: AnswerSection[]; kind?: string;
}
// reviewer: the firm's internal note. client_notice and document_request are letters to the client, drafted by
// code from templates.
export interface Note { client: string; kind: "reviewer" | "document_request" | "client_notice"; route: RouteKey; text: string; source: string }
export interface PackView {
  name: string; version: number; sha: string; tax_year: number; overtime_definition: string; overtime_summary: string;
  overtime_label?: string; // plain words, e.g. "W-2 box 14 total" or "FLSA premium only"
}
// label, from and to come formatted by the API (money as "$12,500", definitions and fact values in words; a
// fact that was empty reads "not on file"). check is the cited authority looked up at its address.
export interface AuthorityCheck { state: "found" | "not_found" | "not_checked"; detail: string; url: string | null }
export interface ChangeEdit {
  kind: "rule" | "fact"; path: string; label?: string; client?: string; from: string | null; to: string; cite?: string; url?: string | null;
  check?: AuthorityCheck;
}
export interface ChangeView { id: string; kind: string; title: string; author: string; rationale: string; document: string | null; client: string | null; edits: ChangeEdit[] }

// A change set exactly as the API returned it; sent back unchanged for approval and replay.
export type ChangeSetJson = Record<string, unknown> & { id: string; kind: string; title: string };
export interface HistoryItem { change: ChangeSetJson; approver: string; materiality: string; receipt: string }

export interface Reading { kind: string; method: string; note: string; clients: string[]; line: string; direction: string; hypothetical: boolean; reason: string }

// One reached return told from the change to its next step, in the API's words: "C-026, a filed married filing
// jointly return. Its overtime deduction would go from $18,000 to $6,000. ..." A preview, a what-if and an
// applied change carry one; a turned-down change and a question about a client do not.
export interface Story { client: string; text: string; change: string }

// A question about a client carries its line's method, worksheet and corrected-return preview at the top level.
export interface RunResult extends Computation {
  kind: "change" | "explain" | "unsupported" | "error";
  // waiting (a preview at Approve), what_if (a preview only, never applied), applied, turned_down, rejected,
  // stopped, answered, unsupported, stale, history_full, error.
  status: string;
  question?: string;
  message?: string;
  reading?: Reading;
  change?: ChangeSetJson;
  change_view?: ChangeView;
  hypothetical?: boolean;
  preview_token?: string;
  history_full?: boolean; // the session already holds the most approved changes the server replays
  history_error?: { index: number; change: string };
  materiality?: string;
  pack_before?: PackView;
  pack_after?: PackView;
  facts_after?: Record<string, number>;
  kpis?: Kpis;
  story?: Story | null;
  impact?: ImpactRow[];
  not_reached?: NotReachedRow[];
  authorities?: Authority[];
  book?: BookRow[];
  audit?: AuditEvent[];
  answer?: Answer | null;
  errors?: { field: string; message: string }[];
  notes?: Note[];
  letters?: Note[]; // on a preview: the letters to clients approval would write, drafted by code
  history_item?: HistoryItem;
  cli?: Record<string, unknown>;
  reason?: string;
  can_do?: string[];
  client?: string; // "" for a question about the whole session, which names a topic instead
  topic?: string; // "amendments" or "waiting"
  line?: string;
  line_label?: string;
  chain?: ChainNode;
  parity?: Parity;
}

// needs: the change set the example only makes sense after ("CS-001").
export interface Example { id: string; step: string; label: string; question: string; needs?: string }

// label: how the service runs, in words ("PolicyEngine-US, live API" or "PolicyEngine-US 2.90.4, cached results").
export interface ServiceStatus { state: string; detail?: string; model?: string; ms?: number; release?: string; cached_at?: string | null; checked_s_ago?: number; label?: string }

// The question reader measured on recorded cases (data/eval_intake.json): with the model over several runs, and
// with the pattern reader alone. Every key is optional: the file has grown, and an older server sends less.
export interface ReaderCounts { correct?: number; per_run?: number[]; read_by_the_model?: number; refused_by_the_checks?: number; wrong_and_accepted?: number }
export interface ReaderSet {
  cases?: number;
  with_model?: ReaderCounts;
  pattern_reader_only?: { correct?: number };
  by_group?: Record<string, { cases?: number; correct?: number }>;
  missed?: string[];
}
// An earlier measurement of one set, before the reader was changed for its misses: per_run [77, 77, 77].
export interface ReaderEarlier { set?: string; recorded?: string; cases?: number; per_run?: number[]; note?: string }
export interface ReaderEval extends ReaderSet {
  model?: string;
  recorded?: string;
  runs?: number;
  sets?: Record<string, ReaderSet | undefined>; // authored, blind, holdout
  earlier?: ReaderEarlier[];
}
export interface Health {
  ok: boolean; version: string; hosted: boolean;
  book: { label: string; tax_year: number; returns: number };
  pack: PackView;
  services: {
    anthropic: ServiceStatus; ecfr: ServiceStatus; policyengine: ServiceStatus; docling: ServiceStatus; ollama: ServiceStatus | null;
    federal_register?: ServiceStatus | null; irs?: ServiceStatus | null;
  };
  limits: { question_chars: number; history: number; runs_per_10_min: number; llm_runs_per_10_min: number };
  signing?: "configured" | "per process";
  instance: { started_at: string; uptime_s: number };
  eval?: ReaderEval | null;
}
export interface Book {
  label: string; tax_year: number; pack: PackView; returns: BookRow[];
  parity: { agree: number; differs: number; not_checked: number };
  statuses: { prep: number; review: number; filed: number };
  drift: Record<string, string[]>; filed: number; parity_note: string;
}

// One run as the browser keeps it: every event of the question and of the decision, in order.
export interface RunRecord {
  id: string;
  question: string;
  at: string;
  events: StreamEvent[];
  receivedAt: number[]; // when each event arrived, ms since the request started
  preview?: RunResult;
  result?: RunResult;
  status: string;
  totalMs: number;
  buffered?: boolean; // the events arrived together: the stream was buffered somewhere on the way
}
