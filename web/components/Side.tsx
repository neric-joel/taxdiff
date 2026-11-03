"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { dollars } from "@/lib/audit";
import { chipName, chipView, grouped, needsOf } from "@/lib/examples";
import { MATERIALITY_HELP, SHORT_FILING, SHORT_STATUS, clockOf } from "@/lib/format";
import { readFlag, writeFlag } from "@/lib/store";
import { summaryOf } from "@/lib/summary";
import type { BookRow, Example, RunRecord } from "@/lib/types";
import { Ids } from "./Ids";

export type Notice = { tone: "error" | "info"; text: string; fields?: { field: string; message: string }[] } | null;
export type SideTab = "ask" | "history";
export interface ApprovedItem { id: string; title: string; approver: string; at: string }

// The client facts a reviewer can change here, with the API's path for each. kind decides the value control.
type FactKind = "money" | "number" | "multiple" | "basis" | "date";
const FACT_FIELDS: { key: string; label: string; kind: FactKind }[] = [
  { key: "wages", label: "W-2 wages (box 1)", kind: "money" },
  { key: "ot_box14", label: "W-2 box 14 overtime pay", kind: "money" },
  { key: "ot_multiple", label: "Overtime pay multiple", kind: "multiple" },
  { key: "ot_basis", label: "Overtime basis", kind: "basis" },
  { key: "ot_flsa_hours", label: "FLSA overtime hours", kind: "number" },
  { key: "ot_regular_rate", label: "Regular rate of pay", kind: "money" },
  { key: "ot_premium_reported", label: "FLSA premium reported by the employer", kind: "money" },
  { key: "ot_premium_paid", label: "Overtime premium paid above the regular rate", kind: "money" },
  { key: "tips_qualified", label: "Qualified tips", kind: "money" },
  { key: "birth_date", label: "Birth date", kind: "date" },
];
const BASIS: [string, string][] = [["flsa", "FLSA"], ["state_or_contract", "State law or contract"], ["mixed", "Mixed"]];
const MAX_FACTS = 8;

interface FactRow { id: number; client: string; person: "0" | "1"; field: string; value: string }

/** The value as the instruction writes it, or why it cannot be written. */
function factValue(row: FactRow): { value: string } | { error: string } {
  if (!row.client) return { error: "Choose a client." };
  const field = FACT_FIELDS.find((item) => item.key === row.field);
  if (!field) return { error: "Choose the fact to change." };
  const raw = row.value.trim();
  if (!raw) return { error: "Enter the new value." };
  if (field.kind === "money" || field.kind === "number") {
    const cleaned = raw.replace(/[$,\s]/g, "");
    return /^\d+(\.\d{1,4})?$/.test(cleaned) ? { value: cleaned } : { error: "Enter a number of zero or more, such as 150 or 149000." };
  }
  if (field.kind === "multiple") return raw === "1.5" || raw === "2" ? { value: raw } : { error: "Choose 1.5 or 2." };
  if (field.kind === "basis") return BASIS.some(([key]) => key === raw) ? { value: raw } : { error: "Choose the overtime basis." };
  return /^\d{4}-\d{2}-\d{2}$/.test(raw) ? { value: raw } : { error: "Enter the date as YYYY-MM-DD." };
}

/** Change one or more client facts: each becomes one `set C-019 people[0].ot_flsa_hours to 150` line in the
 * question box, which the API reads exactly, and the change runs like any other. */
function FactForm({ book, busy, onPreview }: { book: BookRow[]; busy: boolean; onPreview: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<FactRow[]>([{ id: 1, client: "", person: "0", field: "", value: "" }]);
  const [errors, setErrors] = useState<Record<number, string>>({});
  const nextId = useRef(2);
  const clients = useMemo(() => book.length ? book.map((row) => ({ id: row.client, filing: row.filing, status: row.status }))
    : Array.from({ length: 40 }, (_, i) => ({ id: `C-${String(i + 1).padStart(3, "0")}`, filing: "", status: "" })), [book]);
  // Without the book the filing status is unknown, so the spouse stays offered and the server checks it.
  const joint = (client: string) => !book.length || clients.find((item) => item.id === client)?.filing === "mfj";

  useEffect(() => { setOpen(readFlag("facts-open") === "1"); }, []);

  const update = (id: number, patch: Partial<FactRow>) => {
    setRows((list) => list.map((row) => {
      if (row.id !== id) return row;
      const next = { ...row, ...patch };
      if (patch.field !== undefined && patch.field !== row.field) next.value = "";
      if (next.person === "1" && !joint(next.client)) next.person = "0";
      return next;
    }));
    setErrors((found) => { const rest = { ...found }; delete rest[id]; return rest; });
  };
  const add = () => {
    setRows((list) => [...list, { id: nextId.current++, client: list[list.length - 1]?.client ?? "", person: "0", field: "", value: "" }]);
  };
  const remove = (id: number) => setRows((list) => list.filter((row) => row.id !== id));

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const found: Record<number, string> = {};
    const lines: string[] = [];
    for (const row of rows) {
      const checked = factValue(row);
      if ("error" in checked) found[row.id] = checked.error;
      else lines.push(`set ${row.client} people[${row.person}].${row.field} to ${checked.value}`);
    }
    setErrors(found);
    const first = rows.find((row) => found[row.id]);
    if (first) {
      document.getElementById(`fact-${first.id}-${!first.client ? "client" : !first.field ? "field" : "value"}`)?.focus();
      return;
    }
    onPreview(lines.join("\n"));
  };

  return (
    <details className="fact-form" open={open} onToggle={(event) => {
      const now = (event.currentTarget as HTMLDetailsElement).open;
      setOpen(now);
      writeFlag("facts-open", now ? "1" : "0");
    }}>
      <summary>Change a client fact</summary>
      <form onSubmit={submit} noValidate>
        <p className="form-note">Each fact runs as a change to preview; the exact instruction it becomes shows in the question box.</p>
        {rows.map((row, index) => {
          const field = FACT_FIELDS.find((item) => item.key === row.field);
          const error = errors[row.id];
          const described = error ? `fact-${row.id}-error` : undefined;
          return (
            <fieldset key={row.id} className="fact-row">
              <legend className="sr-only">Fact {index + 1}</legend>
              <div>
                <label htmlFor={`fact-${row.id}-client`}>Client</label>
                <select id={`fact-${row.id}-client`} value={row.client} aria-invalid={error && !row.client ? true : undefined} aria-describedby={described}
                        onChange={(event) => update(row.id, { client: event.target.value })}>
                  <option value="">Choose</option>
                  {clients.map((item) => (
                    <option key={item.id} value={item.id}>{item.id}{item.status ? ` · ${SHORT_STATUS[item.status] ?? item.status} · ${SHORT_FILING[item.filing] ?? item.filing}` : ""}</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor={`fact-${row.id}-person`}>Person</label>
                <select id={`fact-${row.id}-person`} value={row.person} onChange={(event) => update(row.id, { person: event.target.value as "0" | "1" })}>
                  <option value="0">Taxpayer</option>
                  {joint(row.client) ? <option value="1">Spouse (joint return)</option> : null}
                </select>
              </div>
              <div className="full">
                <label htmlFor={`fact-${row.id}-field`}>Fact</label>
                <select id={`fact-${row.id}-field`} value={row.field} aria-invalid={error && row.client && !row.field ? true : undefined} aria-describedby={described}
                        onChange={(event) => update(row.id, { field: event.target.value })}>
                  <option value="">Choose</option>
                  {FACT_FIELDS.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
                </select>
              </div>
              <div className="full">
                <label htmlFor={`fact-${row.id}-value`}>New value</label>
                {field?.kind === "multiple" ? (
                  <select id={`fact-${row.id}-value`} value={row.value} aria-invalid={error && row.field ? true : undefined} aria-describedby={described}
                          onChange={(event) => update(row.id, { value: event.target.value })}>
                    <option value="">Choose</option><option value="1.5">1.5</option><option value="2">2</option>
                  </select>
                ) : field?.kind === "basis" ? (
                  <select id={`fact-${row.id}-value`} value={row.value} aria-invalid={error && row.field ? true : undefined} aria-describedby={described}
                          onChange={(event) => update(row.id, { value: event.target.value })}>
                    <option value="">Choose</option>
                    {BASIS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                ) : (
                  <input id={`fact-${row.id}-value`} type={field?.kind === "date" ? "date" : "text"} inputMode={field?.kind === "date" ? undefined : "decimal"}
                         value={row.value} aria-invalid={error && row.field ? true : undefined} aria-describedby={described}
                         placeholder={field?.kind === "date" ? "YYYY-MM-DD" : field?.kind === "number" ? "For example 150" : "For example 149000"}
                         onChange={(event) => update(row.id, { value: event.target.value })} />
                )}
              </div>
              {error ? <p className="field-error full" id={`fact-${row.id}-error`}>{error}</p> : null}
              {rows.length > 1 ? (
                <div className="full"><button type="button" className="link" onClick={() => remove(row.id)}>Remove fact {index + 1}</button></div>
              ) : null}
            </fieldset>
          );
        })}
        <div className="row">
          <button type="submit" className="btn primary small" disabled={busy}>Preview</button>
          <button type="button" className="btn small" onClick={add} disabled={rows.length >= MAX_FACTS}>Add another fact</button>
        </div>
      </form>
    </details>
  );
}

/** The examples in plain words, in four groups. One that needs a change this session has not applied says so,
 * and a click offers to run that change first instead of running a question about the wrong returns. */
function Examples({ examples, historyIds, busy, apiDown, onRun }: {
  examples: Example[] | null; historyIds: string[]; busy: boolean; apiDown: boolean; onRun: (question: string) => void;
}) {
  const [offer, setOffer] = useState<{ example: Example; needs: string } | null>(null);
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (offer) first.current?.focus(); }, [offer]);
  if (examples === null) return <div className="empty">{apiDown ? "The examples load from the server." : "Loading the examples."}</div>;
  const runFirst = (needs: string) => {
    const source = examples.find((item) => !needsOf(item) && (chipView(item).code === needs || item.label.startsWith(`${needs}:`) || item.question.includes(needs)));
    setOffer(null);
    onRun(source?.question ?? `Run ${needs}`);
  };
  return (
    <>
      <h2 className="side-h examples-h">Examples</h2>
      {grouped(examples).map(({ group, items }) => (
        <div className="chip-group" key={group}>
          <h3>{group}</h3>
          <div className="chips">
            {items.map(({ example, view }) => {
              const met = !view.needs || historyIds.includes(view.needs);
              return (
                <button key={example.id} type="button" className="chip" disabled={busy} data-needs={view.needs && !met ? "true" : undefined}
                        aria-label={chipName(view, met)} title={example.question}
                        onClick={() => (met ? onRun(example.question) : setOffer({ example, needs: view.needs as string }))}>
                  <span className="chip-label"><Ids text={view.label} /></span>
                  {view.needs && !met ? <span className="chip-tag unmet">Run <span className="id">{view.needs}</span> first</span> : null}
                  {view.code ? <span className="chip-code">{view.code}</span> : null}
                </button>
              );
            })}
          </div>
          {offer && items.some((item) => item.example === offer.example) ? (
            <div className="offer" role="group" aria-labelledby="offer-text">
              <p id="offer-text">&ldquo;<Ids text={chipView(offer.example).label} />&rdquo; is about the returns after <span className="id">{offer.needs}</span>, which this session has not applied.</p>
              <div className="row">
                <button ref={first} type="button" className="btn small primary" disabled={busy} onClick={() => runFirst(offer.needs)}>Run {offer.needs} first</button>
                <button type="button" className="btn small" onClick={() => setOffer(null)}>Cancel</button>
              </div>
            </div>
          ) : null}
        </div>
      ))}
    </>
  );
}

export function SidePanel({
  tab, onTab, question, onQuestion, questionChars, busy, running, onSubmit, onRun, onPreviewFacts, notice, materiality, onSettings,
  examples, apiDown, historyIds, book, runs, activeId, onReplay, approved, onUndo,
}: {
  tab: SideTab; onTab: (tab: SideTab) => void;
  question: string; onQuestion: (text: string) => void; questionChars: number;
  busy: boolean; running: "question" | "decision" | null;
  onSubmit: () => void; onRun: (text: string) => void; onPreviewFacts: (text: string) => void;
  notice: Notice; materiality: string; onSettings: () => void;
  examples: Example[] | null; apiDown: boolean; historyIds: string[]; book: BookRow[];
  runs: RunRecord[]; activeId: string | null; onReplay: (run: RunRecord) => void;
  approved: ApprovedItem[]; onUndo: (() => void) | null;
}) {
  const onKey = (event: React.KeyboardEvent) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next: SideTab = event.key === "Home" ? "ask" : event.key === "End" ? "history" : tab === "ask" ? "history" : "ask";
    onTab(next);
    document.getElementById(`tab-${next}`)?.focus();
  };
  const last = approved[approved.length - 1];
  return (
    <section className="panel ask" aria-label="Question, client facts and history">
      <div className="tabs" role="tablist" aria-label="Side panel">
        <button type="button" role="tab" id="tab-ask" aria-selected={tab === "ask"} aria-controls="pane-ask" tabIndex={tab === "ask" ? 0 : -1}
                onClick={() => onTab("ask")} onKeyDown={onKey}>Question or change</button>
        <button type="button" role="tab" id="tab-history" aria-selected={tab === "history"} aria-controls="pane-history" tabIndex={tab === "history" ? 0 : -1}
                onClick={() => onTab("history")} onKeyDown={onKey}>History ({runs.length})</button>
      </div>

      <div role="tabpanel" id="pane-ask" aria-labelledby="tab-ask" hidden={tab !== "ask"}>
        <form className="panel-body" onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
          <label htmlFor="question" className="sr-only">Question or change</label>
          <textarea id="question" value={question} maxLength={questionChars} aria-describedby="question-count" onChange={(event) => onQuestion(event.target.value)}
                    placeholder="For example: What if the overtime cap drops to $10,000?"
                    onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); onSubmit(); } }} />
          <div className="count num" id="question-count">{question.length} of {questionChars} characters</div>
          <div className="row">
            <button type="submit" className="btn primary" disabled={!question.trim() || busy}>{running === "question" ? "Running" : "Run"}</button>
            <span className="firm-line"><span className="term" title={`Materiality. ${MATERIALITY_HELP}`}>Amend threshold</span> <b className="num">{dollars(materiality)}</b>, firm setting{" "}
              <button type="button" className="link" onClick={onSettings} aria-label="Change the amend threshold, a firm setting">Change</button></span>
          </div>
          {notice ? (
            <div className={`notice${notice.tone === "error" ? " error" : ""}`} role={notice.tone === "error" ? "alert" : "status"}>
              <Ids text={notice.text} />
              {notice.fields?.length ? <ul>{notice.fields.map((f) => <li key={f.field}>{f.field}: {f.message}</li>)}</ul> : null}
            </div>
          ) : null}
        </form>
        <div className="panel-body fact-wrap">
          <FactForm book={book} busy={busy} onPreview={onPreviewFacts} />
        </div>
        <div className="panel-body examples">
          <Examples examples={examples} historyIds={historyIds} busy={busy} apiDown={apiDown} onRun={onRun} />
        </div>
      </div>

      <div role="tabpanel" id="pane-history" aria-labelledby="tab-history" hidden={tab !== "history"} className="panel-body">
        <h3 className="side-h">Approved in this session</h3>
        {approved.length === 0 ? <p className="side-note">No change is approved yet. The 40 sample returns are at rules version 1 with the original client facts.</p> : (
          <ol className="approved">
            {approved.map((item) => (
              <li key={item.id}>
                <span className="approved-id id">{item.id}</span>
                <span className="approved-title"><Ids text={item.title} /></span>
                <span className="t num">{clockOf(item.at)}{item.approver ? ` · ${item.approver}` : ""}</span>
              </li>
            ))}
          </ol>
        )}
        {last && onUndo ? <button type="button" className="btn small undo" onClick={onUndo} disabled={busy}>Undo {last.id}</button> : null}
        <h3 className="side-h">Runs</h3>
        {runs.length === 0 ? <p className="side-note">Each run is kept here. Pick one to replay its steps in the order they ran.</p> : (
          <ul className="history">
            {[...runs].reverse().map((item) => (
              <li key={item.id}>
                <button type="button" aria-current={item.id === activeId} onClick={() => onReplay(item)} disabled={busy}>
                  <span className="q"><Ids text={item.question} /></span>
                  <span className="t num">{clockOf(item.at)}</span>
                  <span className="r">{summaryOf(item.status, item.result)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
