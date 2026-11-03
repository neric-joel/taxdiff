"use client";

import { useCallback, useEffect, useState } from "react";

import { getBook, getExamples, getHealth, streamRun } from "../lib/api";
import { capFirst, dateOf, lowerFirst, policyEngineLabel } from "../lib/format";
import { formatMs, NODES } from "../lib/graph";
import type { Health, HistoryItem, NodeEvent, NodeId, ReaderEarlier, ReaderEval, RunResult, ServiceStatus, StreamEvent } from "../lib/types";

// Every endpoint of the API, called from this browser, and every outside service the runs use. A run here is
// the same request the dashboard sends; nothing is stored, and the approval below changes nothing anywhere
// (the server keeps no session).

type Outcome = "checking" | "ok" | "note" | "failed";
interface Check { id: string; method: "GET" | "POST"; path: string; what: string; outcome: Outcome; result: string; ms?: number; body?: unknown }
interface Streamed { events: StreamEvent[]; result: RunResult | null; ms: number }
interface ServiceRow { name: string; usedBy: string; how: string; outcome: Outcome; detail: string }

const CHECKS: Omit<Check, "outcome" | "result">[] = [
  { id: "health", method: "GET", path: "/api/health", what: "The rules version, the sample returns, and whether each outside service answers" },
  { id: "examples", method: "GET", path: "/api/examples", what: "The example questions shown on the dashboard" },
  { id: "book", method: "GET", path: "/api/book", what: "The 40 sample returns at the start of a session" },
  { id: "preview", method: "POST", path: "/api/run", what: "A change: Run CS-001 (IRS Notice 2025-69), which stops at Reviewer approval with every route and amount" },
  { id: "approve", method: "POST", path: "/api/approve", what: "The decision on that preview, signed by the server: approve it as \"Status check\"" },
  { id: "explain", method: "POST", path: "/api/run", what: "A question about a client after the approval: Why did C-031's tax go down?" },
  { id: "whatif", method: "POST", path: "/api/run", what: "A what-if: What if the overtime cap drops to $10,000? (previewed, never applied)" },
];

async function collect(path: "/api/run" | "/api/approve", body: unknown): Promise<Streamed> {
  const events: StreamEvent[] = [];
  const started = performance.now();
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 100_000);
  try {
    await streamRun(path, body, { onEvent: (event) => { events.push(event); } }, controller.signal);
  } finally {
    window.clearTimeout(timer);
  }
  const found = events.find((e) => e.type === "result");
  return { events, result: found && found.type === "result" ? found.result : null, ms: performance.now() - started };
}

function stepsOf(events: StreamEvent[]): Partial<Record<NodeId, NodeEvent>> {
  const last: Partial<Record<NodeId, NodeEvent>> = {};
  for (const event of events) if (event.type === "node" && event.state !== "running" && event.state !== "queued") last[event.node] = event;
  return last;
}

/** A service Cite the law checks live: the Federal Register API and irs.gov, when the server reports them. */
function lawRow(name: string, usedBy: string, s: ServiceStatus): ServiceRow {
  const ok = s.state === "ok" || s.state === "live";
  return {
    name,
    usedBy,
    how: ok ? "Live, checked on each run" : s.state === "off" ? "Off on this server: the citation list is used" : "Did not answer: the citation list is used",
    outcome: ok ? "ok" : "note",
    detail: s.detail || s.state,
  };
}

function serviceRows(health: Health | null): ServiceRow[] {
  if (!health) return [];
  const s = health.services;
  const anthropic = s.anthropic;
  const rows: ServiceRow[] = [
    {
      name: `Anthropic API${anthropic.model ? ` (${anthropic.model})` : ""}`,
      usedBy: "Read the question, Write notes, Answer",
      how: anthropic.state === "ok" ? "Live, checked with a model lookup that spends no tokens" : "Off on this server: the pattern reader and fixed templates run instead",
      outcome: anthropic.state === "ok" ? "ok" : anthropic.state === "error" ? "failed" : "note",
      detail: anthropic.detail || anthropic.state,
    },
    {
      name: "eCFR API (ecfr.gov)",
      usedBy: "Cite the law",
      how: s.ecfr.state === "off" ? "Off: the saved excerpts are read" : "Live, with the saved excerpts as the fallback after 3 s",
      outcome: s.ecfr.state === "ok" ? "ok" : "note",
      detail: s.ecfr.detail || s.ecfr.state,
    },
  ];
  if (s.federal_register) rows.push(lawRow("Federal Register API (federalregister.gov)", "Cite the law: related Treasury and IRS documents, searched live", s.federal_register));
  if (s.irs) rows.push(lawRow("irs.gov", "Cite the law: each cited IRS source checked live", s.irs));
  const pe = s.policyengine;
  const cached = pe.state === "cached";
  rows.push(
    {
      name: policyEngineLabel(pe),
      usedBy: "Cross-check (Engine B)",
      how: cached ? "Cached: computed live on a laptop and keyed by each return's inputs (too heavy for a serverless function)"
        : pe.state === "live" ? "Live, through the public PolicyEngine API; the saved results are read only when it does not answer"
        : pe.detail || pe.state,
      outcome: cached || pe.state === "live" ? "ok" : "note",
      // A computed date belongs to cached results only; next to Live it would contradict it.
      detail: cached && pe.cached_at ? `results computed ${dayOf(pe.cached_at.slice(0, 10))}` : pe.state === "live" ? "answering" : pe.detail || pe.state,
    },
    {
      name: "Docling",
      usedBy: "Read documents (PDF statements)",
      how: s.docling.state === "cached" ? "Cached conversion of the PDF statement; the text statement is read live" : "Live",
      outcome: "ok",
      detail: s.docling.state,
    },
    {
      name: health.hosted ? "Vercel" : "Local server",
      usedBy: "Hosting: this page and the API",
      how: health.hosted ? "One Vercel project with two services: the dashboard and the Python API" : "uvicorn on this machine",
      outcome: "ok",
      detail: `instance up ${Math.round(health.instance.uptime_s)} s`,
    },
  );
  if (s.ollama) rows.push({ name: "Ollama (local model)", usedBy: "Read the question, Write notes, Answer on this machine", how: "Local only", outcome: s.ollama.state === "ok" ? "ok" : "note", detail: s.ollama.state });
  return rows;
}

const MARK: Record<Outcome, string> = { checking: "Checking", ok: "Working", note: "Working, with a fallback", failed: "Failed" };

/** A date the API sends as "2026-11-02", read as that calendar day wherever the browser is. */
function dayOf(value: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? dateOf(`${value}T12:00:00`) : dateOf(value);
}

// The recorded sets in the order they were written, named for what each one is.
const SETS: [string, string][] = [
  ["authored", "Written with the reader"],
  ["blind", "Written blind, then used to improve the reader"],
  ["holdout", "Held out, written later"],
];
// How a set's earlier measurement reads beside it.
const EARLIER_WORDS: Record<string, string> = { blind: "before the changes it led to", holdout: "when first measured" };

/** A set's correct readings in each recorded run, each value once: "77", "49 and 50". */
function perRun(values: number[] | undefined): string {
  const seen = [...new Set(values ?? [])];
  if (!seen.length) return "";
  return seen.length === 1 ? String(seen[0]) : `${seen.slice(0, -1).join(", ")} and ${seen[seen.length - 1]}`;
}

/** A set's earlier measurement in words, "77 per run before the changes it led to", with the API's note. */
function earlierOf(set: string, earlier: ReaderEarlier[] | undefined): { text: string; note?: string } | null {
  const found = earlier?.find((item) => item.set === set);
  const runs = perRun(found?.per_run);
  if (!found || !runs) return null;
  return { text: `${runs} per run ${EARLIER_WORDS[set] ?? found.note ?? "earlier"}`, note: found.note };
}

/** How well the question reader reads the recorded cases: with the model over every run, set by set, and with the
 * pattern reader alone. Each figure is the API's; a key the server does not send is left out. */
function ReaderMeasured({ result }: { result: ReaderEval }) {
  const withModel = result.with_model;
  const runs = result.runs ?? withModel?.per_run?.length;
  const groups = Object.entries(result.by_group ?? {});
  const recorded = result.recorded ? dayOf(result.recorded) : "";
  const sets = [
    ...SETS,
    ...Object.keys(result.sets ?? {}).filter((key) => !SETS.some(([known]) => known === key)).map((key): [string, string] => [key, capFirst(key)]),
  ].flatMap(([key, name]) => {
    const set = result.sets?.[key];
    return set ? [{ key, name, set }] : [];
  });
  const lead = withModel?.correct !== undefined && result.cases !== undefined
    ? `With the model: ${withModel.correct} of ${result.cases} read correctly${runs && runs > 1 ? ` in all ${runs} runs` : ""}${
      withModel.read_by_the_model !== undefined
        ? ` (${withModel.read_by_the_model} read by the model${withModel.read_by_the_model < withModel.correct ? ", the rest by code" : ""})` : ""}.`
    : "";
  const alone = result.pattern_reader_only?.correct !== undefined && result.cases !== undefined
    ? ` Without the model (the pattern reader alone): ${result.pattern_reader_only.correct} of ${result.cases}.` : "";
  // A wrong reading the checks accepted would have run. Said for every set only when every set reports it.
  const wrong = sets.map(({ name, set }) => ({ name, n: set.with_model?.wrong_and_accepted }));
  const wrongLine = sets.length && wrong.every((item) => item.n !== undefined)
    ? (wrong.every((item) => item.n === 0) ? "Wrong readings that would have run: 0 in every set and run."
      : `Wrong readings that would have run, by set: ${wrong.map((item) => `${lowerFirst(item.name)}, ${item.n}`).join("; ")}.`)
    : withModel?.wrong_and_accepted !== undefined ? `Wrong readings that would have run: ${withModel.wrong_and_accepted}.` : "";
  return (
    <section className="panel" aria-labelledby="eval-title">
      <div className="panel-head"><h2 id="eval-title">Question reader, measured</h2></div>
      <div className="panel-body">
        {lead || alone ? <p className="eval-line">{lead}{alone}</p> : null}
        {sets.length ? (
          <div className="sheet-wrap">
            <table className="sheet eval-sets">
              <caption className="sr-only">Recorded cases by set, read correctly with the model</caption>
              <thead>
                <tr>
                  <th scope="col">Set</th>
                  <th scope="col" className="num">Cases</th>
                  <th scope="col" className="num">{runs && runs > 1 ? `Correct in all ${runs} runs` : "Read correctly"}</th>
                  <th scope="col" className="hide-phone">Earlier</th>
                </tr>
              </thead>
              <tbody>
                {sets.map(({ key, name, set }) => {
                  const before = earlierOf(key, result.earlier);
                  return (
                    <tr key={key}>
                      <th scope="row">
                        {name}
                        {/* On a phone the earlier measurement sits under the set's name instead of in its own column. */}
                        {before ? <span className="earlier-sub">Earlier: {before.text}</span> : null}
                      </th>
                      <td className="num">{set.cases ?? <span className="dash">-</span>}</td>
                      <td className="num">{set.with_model?.correct ?? <span className="dash">-</span>}</td>
                      <td className="hide-phone" title={before?.note}>{before ? before.text : <span className="dash">-</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {wrongLine ? <p className="eval-line eval-wrong">{wrongLine}</p> : null}
        <p className="panel-note">
          Recorded{recorded ? ` ${recorded}` : ""}{result.model ? ` with ${result.model}` : ""}.{result.missed?.length ? ` Missed: ${result.missed.join(", ")}.` : ""}
        </p>
        {groups.length ? (
          <details className="raw eval-groups">
            <summary>Cases by group, all sets</summary>
            <table className="sheet eval-table">
              <caption className="sr-only">Cases by group, read correctly with the model</caption>
              <thead><tr><th scope="col">Group</th><th scope="col" className="num">Cases</th><th scope="col" className="num">Read correctly</th></tr></thead>
              <tbody>
                {groups.map(([group, counts]) => (
                  <tr key={group}><th scope="row">{group}</th><td className="num">{counts.cases ?? "-"}</td><td className="num">{counts.correct ?? "-"}</td></tr>
                ))}
              </tbody>
            </table>
          </details>
        ) : null}
        <p className="panel-note">
          Each reading the model proposes must quote its numbers, clients and citations from the text, and a pasted passage must quote the
          sentence that states the change, word for word; the replies are recorded and replayed by tests/test_intake_eval.py.
        </p>
      </div>
    </section>
  );
}

export default function StatusPage() {
  const [checks, setChecks] = useState<Check[]>(() => CHECKS.map((c) => ({ ...c, outcome: "checking", result: "" })));
  const [health, setHealth] = useState<Health | null>(null);
  const [steps, setSteps] = useState<{ preview: Partial<Record<NodeId, NodeEvent>>; decision: Partial<Record<NodeId, NodeEvent>> }>({ preview: {}, decision: {} });
  const [running, setRunning] = useState(false);
  const [finishedAt, setFinishedAt] = useState<string>("");

  const set = (id: string, patch: Partial<Check>) => setChecks((all) => all.map((c) => (c.id === id ? { ...c, ...patch } : c)));

  const run = useCallback(async () => {
    setRunning(true);
    setChecks(CHECKS.map((c) => ({ ...c, outcome: "checking", result: "" })));
    setSteps({ preview: {}, decision: {} });
    const timed = async <T,>(id: string, fn: () => Promise<T>, describe: (value: T) => [Outcome, string]) => {
      const started = performance.now();
      try {
        const value = await fn();
        const [outcome, result] = describe(value);
        set(id, { outcome, result, ms: performance.now() - started, body: value });
        return value;
      } catch (error) {
        set(id, { outcome: "failed", result: error instanceof Error ? error.message : String(error), ms: performance.now() - started });
        return null;
      }
    };
    const h = await timed("health", () => getHealth(), (v) => [v.ok ? "ok" : "failed",
      `Rules version ${v.pack.version} · ${v.book.returns} sample returns · approvals signed with ${v.signing === "configured" ? "the shared key" : "a key for this process only"}`]);
    setHealth(h);
    await timed("examples", () => getExamples(), (v) => ["ok", `${v.examples.length} example questions`]);
    await timed("book", () => getBook(), (v) => ["ok", `${v.returns.length} returns · ${v.parity.agree} agree with PolicyEngine-US`]);
    const preview = await timed("preview", () => collect("/api/run", { question: "Run CS-001", history: [], materiality: "50" }),
      (v) => [v.result?.status === "waiting" ? "ok" : "failed",
        `${v.events.length} events streamed · ${v.result?.status === "waiting" ? `waiting at Reviewer approval · ${v.result.kpis?.reached} of ${v.result.kpis?.total} returns reached` : v.result?.status ?? "no result"}`]);
    if (preview) setSteps((s) => ({ ...s, preview: stepsOf(preview.events) }));
    let history: HistoryItem[] = [];
    if (preview?.result?.preview_token && preview.result.change) {
      const r = preview.result;
      const decision = await timed("approve", () => collect("/api/approve", {
        run_id: "", question: r.question ?? "Run CS-001", change: r.change, history: [], materiality: r.materiality ?? "50",
        approver: "Status check", decision: "approve", preview_token: r.preview_token, hypothetical: false,
      }), (v) => [v.result?.status === "applied" ? "ok" : v.result?.status === "history_full" ? "note" : "failed",
        `${v.events.length} events streamed · ${v.result?.status === "applied" ? `applied · rules version ${v.result.pack_after?.version} · ${v.result.notes?.length ?? 0} notes and letters` : v.result?.message ?? v.result?.status ?? "no result"}`]);
      if (decision) setSteps((s) => ({ ...s, decision: stepsOf(decision.events) }));
      if (decision?.result?.history_item) history = [decision.result.history_item as HistoryItem];
    } else {
      set("approve", { outcome: "failed", result: "not run: the preview above did not return a signed token" });
    }
    await timed("explain", () => collect("/api/run", { question: "Why did C-031's tax go down?", history, materiality: "50" }),
      (v) => [v.result?.status === "answered" ? "ok" : "failed", `${v.events.length} events streamed · ${v.result?.status ?? "no result"}${v.result?.answer?.text ? `: ${v.result.answer.text.split(". ")[0]}.` : ""}`]);
    await timed("whatif", () => collect("/api/run", { question: "What if the overtime cap drops to $10,000?", history: [], materiality: "50" }),
      (v) => [v.result?.status === "what_if" ? "ok" : "failed", `${v.events.length} events streamed · ${v.result?.status === "what_if" ? `what-if, nothing applied · ${v.result.kpis?.reached} of ${v.result.kpis?.total} returns would be reached` : v.result?.status ?? "no result"}`]);
    setFinishedAt(new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }));
    setRunning(false);
  }, []);

  useEffect(() => { void run(); }, [run]);

  const working = checks.filter((c) => c.outcome === "ok" || c.outcome === "note").length;
  const services = serviceRows(health);

  return (
    <div className="app status-page">
      <header className="topbar">
        <div className="brand">
          <a className="brand-link" href="/">TaxDiff</a>
          <span className="tagline">System status</span>
        </div>
        <div className="bar-actions">
          <div className="actions">
            <a className="btn small ghost" href="/">Back to the dashboard</a>
            <button type="button" className="btn small" onClick={() => void run()} disabled={running}>{running ? "Checking" : "Check again"}</button>
          </div>
        </div>
      </header>

      <main className="status-main" id="main" tabIndex={-1}>
        <section className="panel" aria-labelledby="endpoints-title">
          <div className="panel-head">
            <h2 id="endpoints-title">API endpoints</h2>
            <span className="end run-status" role="status">{running ? "Checking from this browser" : `${working} of ${checks.length} working · checked ${finishedAt}`}</span>
          </div>
          <p className="panel-sub">Each row is a live request from this browser to the API, the same requests the dashboard sends. The approval is signed by the server and changes nothing stored: the server keeps no session.</p>
          <div className="panel-body table-wrap">
            <table className="data status-table">
              <thead><tr><th scope="col">Endpoint</th><th scope="col">What it does</th><th scope="col">Result</th><th scope="col" className="r">Time</th></tr></thead>
              <tbody>
                {checks.map((c) => (
                  <tr key={c.id} data-outcome={c.outcome}>
                    <td><code className="endpoint"><b>{c.method}</b> {c.path}</code></td>
                    <td>{c.what}</td>
                    <td>
                      <span className={`outcome ${c.outcome}`}>{MARK[c.outcome]}</span>
                      {c.result ? <span className="outcome-detail">{c.result}</span> : null}
                      {c.body !== undefined ? (
                        <details className="raw"><summary>Response</summary><pre>{JSON.stringify(c.body, null, 2).slice(0, 6000)}</pre></details>
                      ) : null}
                    </td>
                    <td className="r num" data-label="Time">{c.ms !== undefined ? formatMs(c.ms) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel" aria-labelledby="services-title">
          <div className="panel-head"><h2 id="services-title">Outside services</h2></div>
          <p className="panel-sub">What each step calls outside this code, as the server reports it on <code>/api/health</code>.</p>
          <div className="panel-body table-wrap">
            {services.length === 0 ? <div className="empty">{running ? "Waiting for /api/health." : "The health check did not answer."}</div> : (
              <table className="data status-table">
                <thead><tr><th scope="col">Service</th><th scope="col">Used by</th><th scope="col">How it runs here</th><th scope="col">State</th></tr></thead>
                <tbody>
                  {services.map((s) => (
                    <tr key={s.name} data-outcome={s.outcome}>
                      <td><b>{s.name}</b></td>
                      <td data-label="Used by">{s.usedBy}</td>
                      <td data-label="How it runs here">{s.how}</td>
                      <td data-label="State"><span className={`outcome ${s.outcome}`}>{MARK[s.outcome]}</span><span className="outcome-detail">{s.detail}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>

        {health?.eval ? <ReaderMeasured result={health.eval} /> : null}

        <section className="panel" aria-labelledby="steps-title">
          <div className="panel-head"><h2 id="steps-title">Workflow steps</h2></div>
          <p className="panel-sub">The {NODES.length} steps of the graph as they ran in the CS-001 preview and its approval above, with the time the server measured.</p>
          <div className="panel-body table-wrap">
            <table className="data status-table">
              <thead><tr><th scope="col">Step</th><th scope="col">Service</th><th scope="col">Preview</th><th scope="col">Approval</th><th scope="col">Summary</th></tr></thead>
              <tbody>
                {NODES.map((node) => {
                  const a = steps.preview[node.id];
                  const b = steps.decision[node.id];
                  const shown = b ?? a;
                  return (
                    <tr key={node.id}>
                      <td><b>{node.name}</b></td>
                      <td data-label="Service">{shown?.provider || node.service}</td>
                      <td data-label="Preview">{a ? <StepState event={a} /> : <span className="muted">{running ? "" : "not in this run"}</span>}</td>
                      <td data-label="Approval">{b ? <StepState event={b} /> : <span className="muted">{running ? "" : "not in this run"}</span>}</td>
                      <td className="summary" data-label="Summary">{shown?.summary ?? ""}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      </main>
      <footer className="footer">Synthetic data only. Every figure on the dashboard comes from code; the model never computes a number.</footer>
    </div>
  );
}

function StepState({ event }: { event: NodeEvent }) {
  const word = event.state === "waiting" ? "Awaiting decision" : event.state === "done" ? "Done" : event.state === "skipped" ? "Skipped" : "Failed";
  return (
    <span className="step-state">
      {word}
      {event.source ? <span className={`badge ${event.source}`}>{event.source === "live" ? "Live" : event.source === "cached" ? "Cached" : event.source === "fallback" ? "Fallback" : event.source}</span> : null}
      {event.duration_ms !== null && event.duration_ms !== undefined && event.state !== "skipped" ? <span className="num muted">{formatMs(event.duration_ms)}</span> : null}
    </span>
  );
}
