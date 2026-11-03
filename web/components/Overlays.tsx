"use client";

import { useEffect, useRef, useState } from "react";

import { dollars } from "@/lib/audit";
import { META, formatMs, stateWord, type NodeView } from "@/lib/graph";
import {
  FILING_LABELS, MATERIALITY_HELP, PARITY_LABELS, ROUTE_LABELS, SOURCE_LABELS, STATUS_LABELS, amountText, capFirst, changeText, dateOf, lineName,
  policyEngineLabel,
} from "@/lib/format";
import type { BookRow, ChainNode, Computation, Health, ImpactRow, NodeId, NotReachedRow } from "@/lib/types";
import { ownAuthorities, sharedAuthority, waitsForDocument, worksheetRows } from "@/lib/worklist";
import { Icon } from "./Icons";
import { Ids } from "./Ids";
import { ChainView, CiteLink, LineNo, RoutePill } from "./Panels";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

/** Modal behaviour: focus moves in (to `initial` when given), Tab stays inside, Escape closes, and focus returns
 * where it was. The dashboard behind is made inert while a modal is open. */
function useModal(ref: React.RefObject<HTMLElement | null>, onClose: () => void, initial?: React.RefObject<HTMLElement | null>) {
  const close = useRef(onClose);
  close.current = onClose;
  const first = useRef(initial);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current;
    const items = () => Array.from(node?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    (first.current?.current ?? items()[0] ?? node)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { close.current(); return; }
      if (event.key !== "Tab" || !node) return;
      const list = items();
      if (list.length === 0) { event.preventDefault(); return; }
      const first = list[0];
      const last = list[list.length - 1];
      if (!node.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); previous?.focus?.(); };
  }, [ref]);
}

/** A side drawer: a modal dialog on the right edge, full width on a phone. */
export function Drawer({ title, onClose, children, label, initialFocus }: {
  title: React.ReactNode; onClose: () => void; children: React.ReactNode; label: string; initialFocus?: React.RefObject<HTMLElement | null>;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useModal(panel, onClose, initialFocus);
  return (
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      <div className="drawer" role="dialog" aria-modal="true" aria-label={label} ref={panel} tabIndex={-1}>
        <div className="drawer-head">
          <h2>{title}</h2>
          <span className="end"><button type="button" className="btn small" onClick={onClose}>Close</button></span>
        </div>
        <div className="drawer-body">{children}</div>
      </div>
    </>
  );
}

/** What a confirmation asks: the question, what the action undoes, item by item, and the button's words. */
export interface ConfirmSpec { title: string; lead: string; items: string[]; confirm: string; onConfirm: () => void }

/** An in-app confirmation for an action that removes work: focus starts on Cancel. */
export function ConfirmDialog({ spec, onClose }: { spec: ConfirmSpec; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useModal(panel, onClose, cancel);
  return (
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-body" ref={panel} tabIndex={-1}>
        <h2 id="confirm-title">{spec.title}</h2>
        <div id="confirm-body">
          <p><Ids text={spec.lead} /></p>
          {spec.items.length ? <ul>{spec.items.map((item) => <li key={item}><Ids text={item} /></li>)}</ul> : null}
        </div>
        <div className="dialog-actions">
          <button type="button" className="btn primary" onClick={spec.onConfirm}>{spec.confirm}</button>
          <button type="button" className="btn" ref={cancel} onClick={onClose}>Cancel</button>
        </div>
      </div>
    </>
  );
}

function Json({ value }: { value: unknown }) {
  if (value === undefined || value === null) return <p className="muted">Nothing recorded for this run.</p>;
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

/** Where the step's result came from: Live, Cached, Fallback or Mock, and the fallback's reason. */
function sourceText(view: NodeView): string {
  const label = view.source ? SOURCE_LABELS[view.source] ?? view.source : "";
  if (!view.fallback) return label || "-";
  if (!label || view.source === "fallback") return `Fallback: ${view.fallback}`;
  return `${label}. Fallback: ${view.fallback}`;
}

export function NodeDrawer({ id, view, onClose }: { id: NodeId; view: NodeView; onClose: () => void }) {
  const meta = META[id];
  const calls = view.request?.calls ?? [];
  const requests = view.request?.requests ?? [];
  return (
    <Drawer label={`${meta.name} step`} onClose={onClose}
            title={<span className="title-with-icon"><Icon name={meta.icon} />{meta.name} <span className="muted" style={{ fontWeight: 400 }}>· {stateWord(view)}</span></span>}>
      <dl className="kv">
        <dt>Service</dt><dd>{view.provider || meta.service}</dd>
        <dt>Source</dt><dd>{sourceText(view)}</dd>
        <dt>Measured time</dt><dd className="num">{view.duration_ms != null ? formatMs(view.duration_ms) : "-"}</dd>
        <dt>Started</dt><dd className="num">{view.started_at ? new Date(view.started_at).toLocaleTimeString("en-US", { hour12: false }) : "-"}</dd>
        <dt>What happened</dt><dd>{view.summary || "Has not run."}</dd>
        {view.error ? (<><dt>Error</dt><dd style={{ color: "var(--negative)" }}>{view.error}</dd></>) : null}
      </dl>
      {calls.length ? (
        <>
          <h3>Model calls</h3>
          <table className="data" style={{ fontSize: 13 }}>
            <thead><tr><th scope="col">Model</th><th scope="col" className="num">Tokens in</th><th scope="col" className="num">Tokens out</th><th scope="col" className="num">Time</th><th scope="col">Outcome</th></tr></thead>
            <tbody>
              {calls.map((call, index) => (
                <tr key={index} style={{ cursor: "default" }}>
                  <td>{call.model ?? call.provider}</td>
                  <td className="num">{call.tokens_in ?? "-"}</td>
                  <td className="num">{call.tokens_out ?? "-"}</td>
                  <td className="num">{call.ms != null ? `${call.ms} ms` : "-"}</td>
                  <td>{call.outcome}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12 }}>The prompt, the reply and the key are not shown or stored; only what each call cost and how it ended.</p>
        </>
      ) : null}
      {requests.length ? (
        <>
          <h3>Requests to outside services</h3>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, lineHeight: "18px" }}>{requests.map((line) => <li key={line} style={{ overflowWrap: "anywhere" }}>{line}</li>)}</ul>
        </>
      ) : null}
      <h3>Input</h3>
      <Json value={view.input} />
      <h3>Output</h3>
      <Json value={view.output} />
    </Drawer>
  );
}

/** How the changed line is computed, in the API's words: the method before and after, the worksheet steps
 * side by side, and for a filed return the corrected-return preview. A part the API does not send is left out. */
function ComputationView({ value }: { value: Computation }) {
  const before = value.method_before;
  const after = value.method;
  const steps = value.worksheet ? worksheetRows(value.worksheet.before ?? [], value.worksheet.after ?? []) : [];
  const amended = value.amended ?? [];
  if (!before && !after && steps.length === 0 && amended.length === 0) return null;
  return (
    <>
      {before || after ? (
        <>
          <h3>How the line is computed</h3>
          <dl className="kv">
            {before ? <><dt>Before</dt><dd>{before}</dd></> : null}
            {after ? <><dt>{before ? "After" : "Method"}</dt><dd>{after}</dd></> : null}
          </dl>
        </>
      ) : null}
      {steps.length ? (
        <div className="sheet-wrap">
          <table className="sheet">
            <caption>Schedule 1-A worksheet, the part that changed</caption>
            <thead><tr><th scope="col">Line</th><th scope="col" className="num">Before</th><th scope="col" className="num">After</th></tr></thead>
            <tbody>
              {steps.map((step) => (
                <tr key={step.key} data-changed={step.before !== step.after}>
                  <th scope="row">
                    <span className="sheet-line">
                      {step.ref ? <span className="lineno">{step.ref}</span> : null}
                      <span>{step.label}{step.beforeLabel ? <span className="sheet-before">Before: {step.beforeLabel}</span> : null}</span>
                    </span>
                  </th>
                  <td className="num">{step.before ?? <span className="dash">-</span>}</td>
                  <td className="num">{step.after ?? <span className="dash">-</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {amended.length ? (
        <div className="sheet-wrap">
          <table className="sheet">
            <caption>Corrected return preview, in Form 1040-X columns</caption>
            <thead><tr><th scope="col">Form 1040 line</th><th scope="col" className="num">A. As filed</th><th scope="col" className="num">B. Change</th><th scope="col" className="num">C. Corrected</th></tr></thead>
            <tbody>
              {amended.map((line) => (
                <tr key={line.line}>
                  <th scope="row"><span className="sheet-line"><span className="lineno">{line.line}</span><span>{line.label}</span></span></th>
                  <td className="num">{line.as_filed}</td>
                  <td className="num">{line.change}</td>
                  <td className="num">{line.corrected}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function ReachedReturn({ row, book }: { row: ImpactRow; book?: BookRow }) {
  const shared = sharedAuthority([row]);
  const by = row.lines[0]?.citation.by;
  return (
    <>
      <dl className="kv">
        <dt>Route</dt>
        <dd>
          <RoutePill route={row.route} />{waitsForDocument(row) && row.route !== "document" ? <span className="muted"> · waiting for a document</span> : null}
          {row.route_reason ? <span className="kv-sub">{capFirst(row.route_reason)}</span> : null}
        </dd>
        {row.conflict ? (<><dt>Conflict</dt><dd><b>{row.conflict}</b></dd></>) : null}
        <dt>Tax, Form 1040 line 16</dt><dd className="num">{amountText(row.tax_before)} to {amountText(row.tax_after)} ({changeText(row.tax_change)}{row.tax_change.range ? " until the document arrives" : ""})</dd>
        {row.vs_filed ? (<><dt>Vs. the filed return</dt><dd className="num">{changeText(row.vs_filed)}</dd></>) : null}
        <dt>PolicyEngine-US</dt><dd>{row.parity ? `${PARITY_LABELS[row.parity.state]}${row.parity.reason && row.parity.state !== "agree" ? `: ${row.parity.reason}` : ""}` : "-"}</dd>
        {row.parity?.authority ? (<><dt>Which is right</dt><dd>{row.parity.authority}</dd></>) : null}
        {row.needs.length ? (<><dt>Waits for</dt><dd>{row.needs.join("; ")}</dd></>) : null}
        {book?.case ? (<><dt>Scenario (synthetic)</dt><dd>{book.case}</dd></>) : null}
      </dl>
      {row.parity?.diffs.length ? (
        <>
          <h3>Where this project&apos;s engine and PolicyEngine-US differ</h3>
          <ul className="drawer-list">
            {row.parity.diffs.map((d) => (
              <li key={d.line}>{d.label}: this project&apos;s engine {d.engine_a}, PolicyEngine-US {d.policyengine}{d.difference ? `, a difference of ${d.difference}` : ""}</li>
            ))}
          </ul>
        </>
      ) : null}
      <ComputationView value={row} />
      <h3>Lines that moved</h3>
      {row.lines.length === 0 ? <p className="muted">One of this return&apos;s line inputs is a rule or fact this change edits, but no line moved.</p> : (
        <>
          <ul className="moved">
            {row.lines.map((line) => {
              const own = shared.every ? [] : ownAuthorities({ ...row, lines: [line] }, shared.cite?.text);
              return (
                <li key={line.line}>
                  <span className="moved-line"><span>{lineName(line.label)}</span><LineNo no={line.no} /></span>
                  <span className="v num">{amountText(line.before)} to {amountText(line.after)}{line.delta ? ` (${line.delta.text})` : ""}</span>
                  {own.length ? <span className="moved-cite">Authority: <CiteLink cite={own[0]} /></span> : null}
                </li>
              );
            })}
          </ul>
          {shared.cite ? <p className="drawer-note">Authority{shared.every ? "" : " for the other lines"}: <CiteLink cite={shared.cite} />{by ? ` (${by})` : ""}</p> : null}
          <details className="inputs">
            <summary>Inputs</summary>
            <p className="drawer-note">Each line lists its inputs: rule parameters with their authority, client facts with where they came from, and the definition version. Inputs this change edits are marked.</p>
            {row.lines.map((line) => (
              <div key={line.line} className="inputs-line">
                <h4><span>{lineName(line.label)}</span><LineNo no={line.no} /></h4>
                <ChainView node={line.chain} />
              </div>
            ))}
          </details>
        </>
      )}
    </>
  );
}

export function ReturnDrawer({ client, row, book, unreached, chain, chainLabel, computation, onClose }: {
  client: string; row?: ImpactRow; book?: BookRow; unreached?: NotReachedRow; chain?: ChainNode; chainLabel?: string; computation?: Computation; onClose: () => void;
}) {
  return (
    <Drawer label={`Return ${client}`} title={<>{client}{book ? <span className="muted" style={{ fontWeight: 400 }}> · {STATUS_LABELS[book.status]} · {FILING_LABELS[book.filing] ?? book.filing}</span> : null}</>} onClose={onClose}>
      {row ? <ReachedReturn row={row} book={book} /> : (
        <>
          <dl className="kv">
            <dt>Reached</dt><dd>{unreached ? `No: ${unreached.reason}` : book?.route ? ROUTE_LABELS[book.route] : "No change has run yet"}</dd>
            {book ? (
              <>
                <dt>Tax, Form 1040 line 16</dt><dd className="num">{amountText(book.tax)}</dd>
                <dt>AGI</dt><dd className="num">{amountText(book.agi)}</dd>
                <dt>Schedule 1-A</dt><dd className="num">{amountText(book.sch1a_total)}</dd>
                <dt>Facts version</dt><dd>v{book.facts_version}</dd>
                <dt>PolicyEngine-US</dt><dd>{PARITY_LABELS[book.parity]}</dd>
                {book.case ? (<><dt>Scenario (synthetic)</dt><dd>{book.case}</dd></>) : null}
              </>
            ) : null}
          </dl>
          {computation ? <ComputationView value={computation} /> : null}
          {chain ? (
            <details className="inputs">
              <summary>Inputs to {client}&apos;s {chainLabel ?? "line"}</summary>
              <ChainView node={chain} />
            </details>
          ) : null}
        </>
      )}
    </Drawer>
  );
}

/** The firm's materiality: shown with an Edit button; a new amount is checked, then confirmed before it is kept. */
function FirmSettings({ materiality, onChange, editRef }: { materiality: string; onChange: (value: string) => void; editRef: React.RefObject<HTMLButtonElement | null> }) {
  const [mode, setMode] = useState<"view" | "edit" | "confirm">("view");
  const [draft, setDraft] = useState(materiality);
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const changed = useRef(false);
  useEffect(() => {
    if (mode === "edit") input.current?.focus();
    else if (mode === "confirm") confirm.current?.focus();
    else if (changed.current) editRef.current?.focus();
  }, [mode, editRef]);
  const review = (event: React.FormEvent) => {
    event.preventDefault();
    const value = draft.trim().replace(/[$,\s]/g, "");
    if (!/^\d+(\.\d{1,2})?$/.test(value) || Number(value) > 1_000_000) {
      setError("Enter an amount from $0 to $1,000,000, with at most two decimals.");
      return;
    }
    if (Number(value) === Number(materiality)) {
      setError(`The amend threshold is already ${dollars(materiality)}.`);
      return;
    }
    setError("");
    setDraft(value);
    setMode("confirm");
  };
  return (
    <section className="settings" aria-labelledby="settings-title">
      <h3 id="settings-title">Firm settings</h3>
      {mode === "view" ? (
        <div className="setting-row">
          <span>Amend threshold (materiality) <b className="num">{dollars(materiality)}</b></span>
          <button ref={editRef} type="button" className="btn small" aria-label="Edit the amend threshold"
                  onClick={() => { setDraft(materiality); setError(""); changed.current = true; setMode("edit"); }}>Edit</button>
        </div>
      ) : mode === "edit" ? (
        <form className="setting-edit" onSubmit={review} noValidate>
          <label htmlFor="materiality">Amend threshold ($)</label>
          <div className="row">
            <input id="materiality" ref={input} type="text" inputMode="decimal" value={draft} aria-invalid={error ? true : undefined}
                   aria-describedby={`materiality-help${error ? " materiality-error" : ""}`} onChange={(event) => setDraft(event.target.value)} />
            <button type="submit" className="btn small primary">Save</button>
            <button type="button" className="btn small" onClick={() => { setError(""); setMode("view"); }}>Cancel</button>
          </div>
          {error ? <p className="field-error" id="materiality-error">{error}</p> : null}
        </form>
      ) : (
        <div className="setting-confirm" role="group" aria-labelledby="materiality-confirm">
          <p id="materiality-confirm">Change the amend threshold from {dollars(materiality)} to {dollars(draft)}? It applies from the next preview; approved changes keep the threshold they were approved with.</p>
          <div className="row">
            <button ref={confirm} type="button" className="btn small primary" onClick={() => { onChange(draft); setMode("view"); }}>Confirm</button>
            <button type="button" className="btn small" onClick={() => setMode("edit")}>Back</button>
          </div>
        </div>
      )}
      <p className="drawer-note" id="materiality-help">{MATERIALITY_HELP} The change is recorded in the audit log.</p>
    </section>
  );
}

export function AboutDrawer({ health, materiality, onMateriality, focusSettings, onClose }: {
  health: Health | null; materiality: string; onMateriality: (value: string) => void; focusSettings?: boolean; onClose: () => void;
}) {
  const edit = useRef<HTMLButtonElement>(null);
  const s = health?.services;
  const model = s?.anthropic.model ?? "claude-haiku-5-5";
  const returns = health?.book?.returns ?? 40;
  const anthropic = s?.anthropic.state;
  const thisServer = !health ? ""
    : anthropic === "ok" ? "This server has a key."
    : anthropic === "error" ? "This server has a key, but the Anthropic API did not answer its last check."
    : anthropic === "other" ? "This server uses a local model instead, so nothing is sent to the Anthropic API."
    : "This server has no key, so nothing is sent.";
  const ecfrLive = s?.ecfr.state !== "off";
  const outside = [s?.federal_register ? "searches the Federal Register API for related Treasury and IRS documents" : "",
    s?.irs ? "checks each cited IRS source on irs.gov" : ""].filter(Boolean);
  const pe = s?.policyengine;
  const peLive = pe?.state === "live";
  const peWhen = dateOf(pe?.cached_at);
  const history = health?.limits?.history ?? 25;
  return (
    <Drawer label="About this prototype" title="About this prototype" onClose={onClose} initialFocus={focusSettings ? edit : undefined}>
      <FirmSettings materiality={materiality} onChange={onMateriality} editRef={edit} />
      <h3>What it computes</h3>
      <p>Part of the tax year 2025 federal Form 1040 for {returns} sample returns, all synthetic: wages, the standard deduction with the age 65 amount, three of the four Schedule 1-A deductions (qualified tips, qualified overtime compensation, the enhanced deduction for seniors) with their caps and phase-outs, taxable income, and regular tax by the Tax Table below $100,000 and the rate schedules above.</p>
      <h3>Not modeled</h3>
      <p>AMT, capital-gain rates, credits, QBI, SALT, car loan interest (Schedule 1-A part IV), state returns, effective dates within a year, e-file; for the standard deduction, blindness, a dependent&apos;s limited amount and the zero amount when a separately filing spouse itemizes.</p>
      <h3>What is live, cached, synthetic or mocked here</h3>
      <dl className="kv">
        <dt>Live</dt><dd>Recompute returns (Engine A), validation, routing and the audit log run on every request. Reading the question, note wording and answers call the Anthropic API ({model}) when this server has a key; otherwise a pattern reader and templates run, and the steps say Fallback. {ecfrLive
          ? "Cite the law fetches 29 CFR 778.107 and 778.110 (Labor Department overtime regulations under the FLSA) from the eCFR API and uses the saved copy if eCFR does not respond."
          : "Cite the law reads saved excerpts of 29 CFR 778.107 and 778.110 (Labor Department overtime regulations under the FLSA), copied from the eCFR API."}
          {outside.length ? ` It also ${outside.join(" and ")}.` : ""}
          {peLive ? " Cross-check runs PolicyEngine-US live through its public API (api.policyengine.org); the saved results are read only when the API does not answer." : ""}</dd>
        <dt>Cached</dt><dd>{peLive
          ? <>Docling&apos;s conversion of the PDF statement, and the saved PolicyEngine-US results that stand in when its API does not answer.</>
          : <>{policyEngineLabel(pe)}{peWhen ? `, computed ${peWhen}` : ""} on a local machine and saved, read here by each return&apos;s inputs; Docling&apos;s conversion of the PDF statement. When you run the project locally, both run live (see the README).</>}</dd>
        <dt>Synthetic</dt><dd>The {returns} returns and the employer statements. No real taxpayer data is used.</dd>
        <dt>Mock</dt><dd>Approvals and sign-offs carry a typed name; there is no sign-in. The audit log lives in this browser tab only; a firm deployment would keep it in its database.</dd>
      </dl>
      <h3>What leaves the server</h3>
      <p>When the server has an Anthropic API key it sends three kinds of text to the Anthropic API: the question you type (Read the question), up to 8 internal-note templates per approval with their client IDs and amounts (Write notes), and the answer template (Answer). Letters to clients and return files are never sent. Every return here is synthetic. {thisServer}</p>
      <p>The approver&apos;s name is typed, not verified; a firm deployment would use its sign-in.</p>
      <h3>How a number is checked</h3>
      <p>Every figure comes from Engine A. The model ({model}, through the Anthropic API) only proposes a change set (refused unless every number, client and citation is in your text) and rewords notes and answers (kept only if every figure, ID and sign survives); it is never asked to compute. PolicyEngine-US recomputes the same returns as a check on the arithmetic; agreement shows both engines do the same arithmetic on the same reading of the law, not that the reading is right.</p>
      <h3>Session</h3>
      <p>The server keeps nothing between requests. Your approved changes stay in this tab with signed receipts, and every request replays them from rules version 1, so a change the server did not approve cannot enter the returns. A session replays at most {history} approved changes; Reset starts again from rules version 1.</p>
      <h3>Words used here</h3>
      <dl className="kv glossary">
        <dt>Rules version (rule pack)</dt><dd>The versioned tax year 2025 parameters (caps, phase-outs, rate schedules, standard deductions), each with its citation. Each approved rule change makes the next version.</dd>
        <dt>Change set (CS-…)</dt><dd>One proposed edit to rule parameters or client facts, with its author, reason and citation. It is previewed first; only an approved change set is applied.</dd>
        <dt>What-if</dt><dd>A question such as &ldquo;What if the overtime cap drops to $10,000?&rdquo;, or a change cited as hypothetical. It shows the preview and applies nothing.</dd>
        <dt>Engine A</dt><dd>This project&apos;s engine, in the Recompute returns step. It computes every figure on the screen.</dd>
        <dt>Engine B</dt><dd>PolicyEngine-US, an independent open-source tax model, in the Cross-check step.</dd>
        <dt>Cite the law</dt><dd>The authorities for the lines a change moves: eCFR sections, IRS sources and related Federal Register documents.</dd>
        <dt>Reached / Not reached</dt><dd>A return is reached when one of its line inputs is a rule or fact the change edits; the others are proven unaffected from those recorded inputs and a full recompute.</dd>
        <dt>Route</dt><dd>Where a reached return goes next: {ROUTE_LABELS.update} (in preparation), {ROUTE_LABELS.reviewer} (in review, or when the engines differ), {ROUTE_LABELS.amendment} (filed), {ROUTE_LABELS.document} (a fact is missing), {ROUTE_LABELS.below_materiality} or {ROUTE_LABELS.no_change} (nothing changed). A return that waits for a fact also gets a document request, whatever its route.</dd>
        <dt>Amend threshold (materiality)</dt><dd>The smallest difference from the filed return that sends a filed return to amendment review. A smaller difference is below materiality: the client is told, and there is no amendment review. It is a firm setting, at the top of this panel.</dd>
        <dt>Sign-off</dt><dd>After a change is applied, each reached return is signed off by role: the preparer for {ROUTE_LABELS.update}, a reviewer for {ROUTE_LABELS.reviewer} and {ROUTE_LABELS.document}, a partner for {ROUTE_LABELS.amendment}; for a return below materiality the reviewer records the client&apos;s choice. A rule change takes a technical approval; a fact or document change takes a reviewer&apos;s approval.</dd>
        <dt>Live / Cached / Fallback / Mock</dt><dd>Live: ran during this request. Cached: read from results saved earlier. Fallback: the preferred service was not available, so a pattern reader, a template or saved excerpts ran instead. Mock: stands in for something a firm would supply, such as sign-in.</dd>
      </dl>
    </Drawer>
  );
}
