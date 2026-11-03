"use client";

import { useRef, useState } from "react";

import { dollars } from "@/lib/audit";
import { MATERIALITY_HELP, capFirst, changeText, minus, plural } from "@/lib/format";
import type { AuthorityCheck, Differs, ImpactRow, Kpis, Move, RunResult } from "@/lib/types";
import { LETTER_KIND, groupByRoute, noteCounts, waitsForDocument } from "@/lib/worklist";
import { Ids } from "./Ids";
import { useArrival } from "./Motion";
import { CiteLink, ClientList } from "./Panels";

interface Differ { client: string; lines: string[]; reason: string }

/** One entry per return the cross-check disagrees on: the API lists each line that differs, and the lines of one
 * return share its reason. */
function differsByReturn(items: Differs[] | undefined, rows: ImpactRow[]): Differ[] {
  const out = new Map<string, Differ>();
  if (items) {
    for (const item of items) {
      const entry = out.get(item.client) ?? { client: item.client, lines: [], reason: item.reason };
      if (item.no && !entry.lines.includes(item.no)) entry.lines.push(item.no);
      out.set(item.client, entry);
    }
  } else {
    for (const row of rows) if (row.parity?.state === "differs") out.set(row.client, { client: row.client, lines: [], reason: row.parity.reason ?? "" });
  }
  return [...out.values()];
}

/** "line 21", "lines 15 and 16", "lines 15, 16 and 1-A 21". */
function linesText(lines: string[]): string {
  if (!lines.length) return "";
  if (lines.length === 1) return `line ${lines[0]}`;
  return `lines ${lines.slice(0, -1).join(", ")} and ${lines[lines.length - 1]}`;
}

/** What looking up a cited authority found, in one line: "Found: Notice 2025-69 is on irs.gov". The lookup shows the
 * authority exists; that it sets this value is the approver's to confirm. */
function checkText(check: AuthorityCheck): string {
  if (check.state === "not_found") return capFirst(check.detail);
  if (check.state === "not_checked") return `Not checked: ${check.detail}`;
  let host = "";
  try {
    host = check.url ? new URL(check.url).hostname.replace(/^www\./, "") : "";
  } catch {
    host = "";
  }
  return `Found: ${check.detail || "the authority"} ${host ? `is on ${host}` : "exists"}; confirm it sets this value`;
}

/** The approval a change needs: a rule change is a technical decision; a fact or a document is a reviewer's. */
export function approvalKind(kind: string | undefined): string {
  return kind === "rule" ? "Technical approval of the rule change" : "Reviewer approval";
}

function MoveRow({ label, move }: { label: string; move: Move }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className="num">{plural(move.returns, "return")}</dd>
      <dd className="num amt">{move.returns ? minus(move.amount.text) : <span className="dash">-</span>}</dd>
    </div>
  );
}

/** The tax rows, each with its count and sum, so a refund never hides more tax elsewhere: more tax, less tax,
 * waiting for a document, and for filed returns the change against the tax as filed. A return whose tax was
 * unknown and is now known is said in words rather than as "0 returns". */
export function TaxRows({ kpis }: { kpis: Kpis }) {
  const moves = kpis.moves;
  const filed = kpis.filed_moves;
  const settled = kpis.settled?.returns ? kpis.settled : null;
  const unknown = kpis.unknown_moves?.returns ?? kpis.tax_change_unknown ?? 0;
  const showMoves = !!moves && (!settled || moves.more.returns + moves.less.returns > 0);
  const filedCount = filed ? filed.more.returns + filed.less.returns : 0;
  if (!moves && !settled && !kpis.tax_change && !unknown && !kpis.conflict_moves?.returns) return null;
  return (
    <>
      <dl className="tax-rows">
        {settled ? (
          <div className="known">
            <dt>Now known</dt>
            <dd className="num">{settled.clients.join(", ")}</dd>
            <dd className="num amt words">
              {settled.vs_filed ? `${settled.vs_filed.text} vs. the filed ${settled.returns === 1 ? "return" : "returns"}` : plural(settled.returns, "return")}
            </dd>
          </div>
        ) : null}
        {showMoves && moves ? (
          <>
            <MoveRow label="More tax" move={moves.more} />
            <MoveRow label="Less tax" move={moves.less} />
          </>
        ) : null}
        {!moves && kpis.tax_change ? (
          <div><dt>Tax change</dt><dd className="num" /><dd className="num amt">{minus(kpis.tax_change.text)}</dd></div>
        ) : null}
        {unknown ? (
          <div>
            <dt>Waiting for a document</dt>
            <dd className="num">{plural(unknown, "return")}</dd>
            <dd className="num amt">{kpis.unknown_moves?.range ? kpis.unknown_moves.range.text : "unknown"}</dd>
          </div>
        ) : null}
        {filed && filedCount > 0 ? (
          <>
            <MoveRow label="Filed returns owing more" move={filed.more} />
            <MoveRow label="Filed returns due a refund" move={filed.less} />
          </>
        ) : null}
        {kpis.conflict_moves?.returns && kpis.conflict_moves.amount ? (
          <div className="attention">
            <dt>Conflicts, priced under the Notice</dt>
            <dd className="num">{kpis.conflict_moves.clients.join(", ")}</dd>
            <dd className="num amt">{minus(kpis.conflict_moves.amount.text)}</dd>
          </div>
        ) : null}
      </dl>
      <p className="tax-note">
        + means more tax owed.{filedCount > 0 ? " Filed returns are measured against the filed return." : ""}
        {unknown && kpis.unknown_moves?.range ? " A range is the least and the most the tax can change until the documents arrive." : ""}
      </p>
    </>
  );
}

/** The amount beside a client in the approval summary: against the tax as filed for a filed return. */
function rowAmount(row: ImpactRow): string {
  return changeText(row.vs_filed ?? row.tax_change);
}

function Clients({ rows, amounts, onClient }: { rows: ImpactRow[]; amounts: boolean; onClient: (client: string) => void }) {
  return (
    <>
      {rows.map((row, index) => (
        // Each client and its amount stay together; a line breaks only after a comma.
        <span key={row.client}>
          <span className="nowrap">
            <button type="button" className="fig" onClick={() => onClient(row.client)} title={`Show ${row.client}`}>{row.client}</button>
            {amounts ? <span className="num"> {row.conflict && rowAmount(row) === "$0" ? "(conflict, see below)" : rowAmount(row)}</span> : null}
            {index < rows.length - 1 ? "," : ""}
          </span>
          {index < rows.length - 1 ? " " : ""}
        </span>
      ))}
    </>
  );
}

export function ApprovalGate({ preview, factsBefore, stale, full, limit, busy, firmMateriality, onDecide, onReset, onClient, onSettings }: {
  preview: RunResult; factsBefore: Record<string, number>; stale: boolean; full: boolean; limit: number; busy: boolean; firmMateriality: string;
  onDecide: (decision: "approve" | "reject", approver: string) => void; onReset: () => void; onClient: (client: string) => void; onSettings: () => void;
}) {
  const [name, setName] = useState("");
  // Which button was pressed, so the card says what it is doing while the decision runs.
  const [sent, setSent] = useState<"approve" | "reject" | null>(null);
  const view = preview.change_view;
  const kpis = preview.kpis;
  const rows = preview.impact ?? [];
  const blank = name.trim().length === 0;
  const groups = groupByRoute(rows);
  const requests = rows.filter(waitsForDocument);
  const alsoAmend = requests.filter((row) => row.route === "amendment").map((row) => row.client);
  const letters = preview.letters ?? [];
  const counts = noteCounts(rows, preview.letters);
  const cites = [...new Map((view?.edits ?? []).filter((edit) => edit.cite).map((edit) => [edit.cite as string, { text: edit.cite as string, url: edit.url || null, check: edit.check }])).values()];
  const from = preview.pack_before?.version;
  const to = preview.pack_after?.version;
  const factClients = [...new Set((view?.edits ?? []).filter((edit) => edit.kind === "fact" && edit.client).map((edit) => edit.client as string))];
  const conflicts = rows.filter((row) => row.conflict);
  const differs = differsByReturn(kpis?.differs, rows);
  const kind = approvalKind(view?.kind ?? (preview.change?.kind as string | undefined));
  const materiality = preview.materiality;
  const moved = materiality !== undefined && Number(materiality) !== Number(firmMateriality);
  return (
    <section className="panel gate" aria-labelledby="gate-title" id="approval">
      <div className="panel-head"><h2 id="gate-title" tabIndex={-1}>{view ? `Approve ${view.id}?` : "Approve this change?"}</h2></div>
      <p className="panel-sub status-line" role="status">
        <span className="approval-kind">{kind}.</span>{" "}
        {busy && sent === "approve" ? "Applying the change: writing the notes, the letters and the audit entry."
          : busy && sent === "reject" ? "Turning the change down: writing the audit entry."
          : "Preview. Nothing changes until you approve."}
      </p>
      {view ? <p className="gate-change"><Ids text={view.title} /></p> : null}
      {view?.rationale ? <p className="gate-why"><Ids text={view.rationale} /></p> : null}
      {/* The decision bar sticks to the foot of the screen within this body only, so it never covers the title. */}
      <div className="gate-body">
      <div className="gate-cols">
        <div className="gate-col">
          <h3>What you approve</h3>
          <dl className="gate-facts">
            {view?.edits.map((edit, index) => (
              // The API words each edit; the parameter or fact path stays in the tooltip for whoever needs it.
              <div key={`${edit.kind}-${edit.client ?? ""}-${edit.path}-${index}`} title={edit.path}>
                <dt>{edit.kind === "fact" ? `${edit.client ? `${edit.client}, ` : ""}${edit.label ?? edit.path}` : capFirst(edit.label ?? edit.path)}</dt>
                <dd>{edit.from ?? (edit.kind === "rule" ? "not set" : "not on file")} to <b>{edit.to}</b></dd>
              </div>
            ))}
            {view?.document ? <div><dt>Source</dt><dd>Employer statement on file{view.client ? ` for ${view.client}` : ""}</dd></div> : null}
            {cites.map((cite) => (
              <div key={cite.text}>
                <dt>Authority</dt>
                <dd>
                  <CiteLink cite={cite} />
                  {cite.check ? (
                    <span className={`check ${cite.check.state}`}>
                      {checkText(cite.check)}
                    </span>
                  ) : null}
                </dd>
              </div>
            ))}
            {from !== undefined ? <div><dt>Rules</dt><dd>{to !== undefined && to !== from ? `version ${from} to version ${to}` : `stay at version ${from}`}</dd></div> : null}
            {factClients.map((client) => (
              <div key={client}><dt>Client facts</dt><dd><span className="id">{client}</span> version {factsBefore[client] ?? 1}{preview.facts_after?.[client] ? ` to ${preview.facts_after[client]}` : ""}</dd></div>
            ))}
            {materiality !== undefined ? (
              <div>
                <dt><span className="term" title={`Materiality. ${MATERIALITY_HELP}`}>Amend threshold</span></dt>
                <dd>
                  <span className="num">{dollars(materiality)}</span> (firm setting){" "}
                  <button type="button" className="link" onClick={onSettings} aria-label="Change the amend threshold, a firm setting">Change</button>
                  {moved ? <span className="cell-sub">The firm setting is now {dollars(firmMateriality)}. Run the change again to preview it at that amount.</span> : null}
                </dd>
              </div>
            ) : null}
          </dl>
          {kpis ? (
            <>
              <h3 className="gate-sub" title="Form 1040 line 16">Change in tax owed</h3>
              <TaxRows kpis={kpis} />
            </>
          ) : null}
        </div>
        <div className="gate-col">
          <h3>What approving does</h3>
          <dl className="gate-facts">
            {groups.filter((group) => group.route === "amendment").map((group) => (
              <div key={group.route}><dt>{group.label}</dt><dd><Clients rows={group.rows} amounts onClient={onClient} /></dd></div>
            ))}
            {requests.length ? (
              <div>
                <dt>Letters asking for documents</dt>
                <dd>
                  <Clients rows={requests} amounts={false} onClient={onClient} />
                  {alsoAmend.length ? <span className="cell-sub"><Ids text={`${alsoAmend.join(", ")} ${alsoAmend.length === 1 ? "is" : "are"} filed, so ${alsoAmend.length === 1 ? "it is" : "they are"} in amendment review too.`} /></span> : null}
                </dd>
              </div>
            ) : null}
            {groups.filter((group) => group.route !== "amendment" && group.route !== "document").map((group) => (
              <div key={group.route}><dt>{group.label}</dt><dd>{plural(group.rows.length, "return")}</dd></div>
            ))}
            {conflicts.length ? (
              <div className="attention">
                <dt>Conflicts</dt>
                <dd>
                  <ul className="gate-list">
                    {conflicts.map((row) => <li key={row.client}><ClientList clients={[row.client]} onClient={onClient} />: <Ids text={row.conflict ?? ""} /></li>)}
                  </ul>
                </dd>
              </div>
            ) : null}
            {differs.length ? (
              <div className="attention">
                <dt>Cross-check differs</dt>
                <dd>
                  <ul className="gate-list">
                    {differs.map((item) => (
                      <li key={item.client}>
                        <ClientList clients={[item.client]} onClient={onClient} />{item.lines.length ? `, ${linesText(item.lines)}` : ""}
                        {item.reason ? <>: <Ids text={item.reason} /></> : null}
                      </li>
                    ))}
                  </ul>
                </dd>
              </div>
            ) : null}
          </dl>
          <p className="gate-writes">
            {rows.length ? `Writes ${plural(counts.internal, "internal note")}, ${plural(counts.letters, "client letter")} and an audit entry.` : "Writes an audit entry."}
          </p>
          {letters.length ? (
            <details className="letters-preview">
              <summary>{letters.length === 1 ? "Preview the letter" : `Preview the ${letters.length} letters`}</summary>
              <ul className="letters">
                {letters.map((letter, index) => (
                  <li key={`${letter.client}-${letter.kind}-${index}`}>
                    <div className="letter-head"><b className="id">{letter.client}</b><span className="muted">Letter to client, {LETTER_KIND[letter.kind] ?? "letter"}</span></div>
                    <p><Ids text={letter.text} /></p>
                  </li>
                ))}
              </ul>
              <p className="fineprint">Drafted by code from fixed templates; approval writes them exactly as shown.</p>
            </details>
          ) : null}
        </div>
      </div>
      {full || stale ? (
        <div className="gate-notices">
          {full ? (
            <div className="notice">
              This session holds {limit} approved changes, the most it can replay. Reset the session to approve more.
              <div style={{ marginTop: 8 }}><button type="button" className="btn small" onClick={onReset} disabled={busy}>Reset the session</button></div>
            </div>
          ) : null}
          {stale ? <div className="notice">The session changed after this preview. Run the change again to approve it.</div> : null}
        </div>
      ) : null}
      <div className="gate-bar">
        <label htmlFor="approver">Your name, recorded in the audit log</label>
        <div className="bar-row">
          <input id="approver" type="text" autoComplete="name" maxLength={80} value={name} onChange={(event) => setName(event.target.value)}
                 placeholder="Your name" disabled={busy || stale} />
          <button type="button" className="btn primary" aria-label={busy && sent === "approve" ? "Applying" : "Approve and apply"} disabled={blank || busy || stale || full}
                  aria-describedby={blank ? "gate-hint" : undefined} onClick={() => { setSent("approve"); onDecide("approve", name); }}>
            {busy && sent === "approve" ? "Applying..." : <><span className="on-wide">Approve and apply</span><span className="on-phone">Approve</span></>}
          </button>
          <button type="button" className="btn" disabled={blank || busy || stale} aria-describedby={blank ? "gate-hint" : undefined}
                  onClick={() => { setSent("reject"); onDecide("reject", name); }}>{busy && sent === "reject" ? "Turning down..." : "Turn down"}</button>
        </div>
        {blank && !stale ? <p className="sr-only" id="gate-hint">Type your name to approve or turn down.</p> : null}
      </div>
      </div>
    </section>
  );
}

/** What replaces the approval panel once the reviewer decides: one line, focused, with a way to the answer and,
 * for the session's latest change, a way to undo it. Its check or cross says the outcome at a glance; when the
 * decision has just been made (arrive) the line slides into the approval's place and the mark draws in. */
export function DecisionLine({ text, outcome, arrive, onAnswer, undo }: {
  text: string; outcome: "applied" | "turned_down"; arrive: object | null; onAnswer: () => void; undo?: { id: string; onUndo: () => void } | null;
}) {
  const ref = useRef<HTMLParagraphElement>(null);
  const phase = useArrival(ref, arrive);
  return (
    <p className="panel decision-line" id="decision" ref={ref} tabIndex={-1} data-outcome={outcome} data-arrive={phase}>
      <svg className="decision-mark" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth={1.8}
           strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {outcome === "applied" ? <path pathLength={1} d="M3.2 8.6l3.1 3.1 6.5-7" />
          : <><path pathLength={1} d="M4.3 4.3l7.4 7.4" /><path pathLength={1} d="M11.7 4.3l-7.4 7.4" /></>}
      </svg>
      <Ids text={text} />
      {" · "}
      <a href="#answer-title" onClick={(event) => { event.preventDefault(); onAnswer(); }}>View answer</a>
      {undo ? <>{" · "}<button type="button" className="link" onClick={undo.onUndo}>Undo {undo.id}</button></> : null}
    </p>
  );
}
