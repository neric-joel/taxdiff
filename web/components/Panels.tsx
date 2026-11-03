"use client";

import { useMemo, useRef, useState } from "react";

import { saveFile } from "@/lib/files";
import {
  PARITY_LABELS, ROUTE_LABELS, ROUTE_ORDER, SHORT_STATUS, amountText, capFirst, changeText, clockOf, filingWords, lineName, lineRef,
  lowerFirst, minus, plural,
} from "@/lib/format";
import { at, type Arrival } from "@/lib/motion";
import { DECISION_WORDS, choicesFor, controlLabel, progress, roleFor, type SignDecision, type SignOff } from "@/lib/signoff";
import type { AnswerPart, AnswerSection, BookRow, ChainNode, ChainRead, ImpactLine, ImpactRow, Kpis, Link, NotReachedRow, RouteKey, RunResult } from "@/lib/types";
import { differsBadge, groupByRoute, ownAuthorities, primaryLine, sharedAuthority, waitsForDocument, worklistCsv, type Cite } from "@/lib/worklist";
import { Ids } from "./Ids";
import { useArrival } from "./Motion";

const FIGURE = /([+-]?)\$?(\d[\d,]*(?:\.\d+)?)/g; // the same pattern as notes.figures in the engine
export const WAITING = "Waiting for the run.";

/** True for text a model reworded (the API's source starts with "template" when the template was kept). */
export function isWorded(source: string | undefined): boolean {
  return !!source && !source.startsWith("template");
}

/** The reason a template was kept, from a source such as "template (the rewording changed a figure)". */
export function templateReason(source: string): string {
  return source.startsWith("template (") && source.endsWith(")") ? source.slice(10, -1) : "";
}

export function RoutePill({ route }: { route: RouteKey | "" }) {
  if (!route) return <span className="dash">-</span>;
  return <span className={`pill ${route}`}>{ROUTE_LABELS[route] ?? route}</span>;
}

export function LineNo({ no, title }: { no: string; title?: string }) {
  return no ? <span className="lineno" title={title ?? lineRef(no)}>{no}</span> : null;
}

/** An authority, linked to its source when the API gives one. */
export function CiteLink({ cite }: { cite: Cite }) {
  return cite.url ? <a href={cite.url} target="_blank" rel="noreferrer">{cite.text}</a> : <>{cite.text}</>;
}

/** A return's status and filing status: in full on a wide screen, short on a phone. */
export function Filing({ status, filing }: { status: string; filing: string }) {
  const words = filingWords(status, filing);
  return (
    <span className="cell-sub filing">
      <span className="on-wide">{words.long}</span>
      <span className="on-phone">{words.short}</span>
    </span>
  );
}

/** Client ids as buttons that open the return, separated by commas. */
export function ClientList({ clients, onClient }: { clients: string[]; onClient: (client: string) => void }) {
  return (
    <>
      {clients.map((client, index) => (
        <span key={client}>
          <button type="button" className="fig id" onClick={() => onClient(client)} title={`Show ${client}`}>{client}</button>
          {index < clients.length - 1 ? ", " : ""}
        </span>
      ))}
    </>
  );
}

// The book map ---------------------------------------------------------------------------------------------

const STATUSES: ("prep" | "review" | "filed")[] = ["prep", "review", "filed"];

/** The book as squares, one per return, coloured by its route when the change reaches it. Every return is
 * also in the tables, which keyboard and screen reader users reach, so the map is a picture for the mouse:
 * hidden from assistive technology and out of the tab order. When a result arrives (arrive), each reached square
 * lights up in its route's colour, in the impact list's order, so the reach spreads through the book. */
export function BookMap({ book, arrive, onCell }: { book: BookRow[]; arrive?: Arrival | null; onCell: (client: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const phase = useArrival(ref, arrive?.result);
  return (
    <div className="bookmap" ref={ref} aria-hidden="true" data-arrive={phase}>
      <div className="bookmap-caption">{book.length} sample returns</div>
      {STATUSES.map((status) => (
        <div className="row" key={status}>
          <span>{SHORT_STATUS[status]}</span>
          <div className="cells">
            {book.filter((row) => row.status === status).map((row) => (
              <span key={row.client} className={`cell${row.route ? ` route-${row.route}` : ""}`}
                    data-reached={row.route ? "true" : "false"} data-route={row.route || undefined}
                    style={row.route ? at(arrive?.delays.get(row.client)) : undefined}
                    title={`${row.client} · ${row.route ? ROUTE_LABELS[row.route] : "not touched"}`}
                    onClick={() => onCell(row.client)} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

// The reach strip: how many returns, where they go, the cross-check, and the returns not reached -------------

export interface CrossCheck { label: string; note?: string | null }

/** The returns a change does not reach, each with its reason, under a toggle in the strip. */
function NotReached({ rows, total, unexpected, onRow, hidden }: {
  rows: NotReachedRow[]; total?: number; unexpected?: number; onRow: (client: string) => void; hidden: boolean;
}) {
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const shown = all ? rows : rows.slice(0, 6);
  const moved = unexpected ?? rows.filter((row) => row.moved).length;
  const one = rows.length === 1;
  const recompute = `A full recompute of ${total ? `all ${total} returns` : "every return"} found ${
    moved === 0 ? (one ? "no change in this return" : "no change in any of these returns") : `a change in ${moved} of these returns`}.`;
  const toggle = (client: string) => setOpen((set) => {
    const next = new Set(set);
    if (next.has(client)) next.delete(client);
    else next.add(client);
    return next;
  });
  return (
    <div className="unreached" id="unreached-body" hidden={hidden}>
      <p className="panel-note">
        {rows.length === 0 ? "Every return uses a rule or fact this change edits."
          : `${one ? "The other return uses" : `The other ${rows.length} returns use`} no rule or fact this change edits. ${recompute}`}
      </p>
      {rows.length ? (
        <>
          <ul className="list">
            {shown.map((row) => {
              // A long reason shows two lines, then the rest on request.
              const long = row.reason.length > 110;
              const full = open.has(row.client);
              return (
                <li key={row.client}>
                  <button type="button" className="row-open who id" aria-label={`Open return ${row.client}`} onClick={() => onRow(row.client)}>{row.client}</button>
                  <span>
                    <span className={`why${long && !full ? " clamp" : ""}`}><Ids text={row.reason} /></span>
                    {long ? <button type="button" className="link more-why" aria-expanded={full} onClick={() => toggle(row.client)}>{full ? "less" : "more"}</button> : null}
                    {row.moved ? <b> · changed in the full recompute; the change was stopped</b> : null}
                  </span>
                </li>
              );
            })}
          </ul>
          {rows.length > 6 ? <button type="button" className="link more" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${rows.length}`}</button> : null}
        </>
      ) : null}
    </div>
  );
}

/** One compact strip: returns reached, the routes, the book map and one cross-check line. The tax amounts live in
 * the approval card while a preview waits and in the answer after a decision, not here. */
export function ReachStrip({ kpis, rows, notReached, book, label, pending, crossCheck, arrive, onClient }: {
  kpis?: Kpis; rows?: ImpactRow[]; notReached?: NotReachedRow[]; book: BookRow[]; label: string; pending: boolean;
  crossCheck: CrossCheck; arrive?: Arrival | null; onClient: (client: string) => void;
}) {
  const [unreachedOpen, setUnreachedOpen] = useState(false);
  const reached = kpis?.reached ?? 0;
  const routes = kpis ? ROUTE_ORDER.filter((key) => (kpis.routes[key] ?? 0) > 0) : [];
  const checked = kpis?.parity_reached;
  const differClients = [...new Set(kpis?.differs?.map((item) => item.client)
    ?? (rows ?? []).filter((row) => row.parity?.state === "differs").map((row) => row.client))];
  return (
    <section className="panel reach-strip" aria-labelledby="kpi-title" aria-busy={pending || undefined}>
      <div className="strip">
        <div className="strip-count">
          <h2 id="kpi-title">Reach summary</h2>
          <p className="reach-count num">{kpis ? <>{kpis.reached}<span className="of"> of {kpis.total}</span></> : <span className="dash">-</span>}</p>
          <p className="strip-sub">{kpis ? `returns this change touches · ${label}` : pending ? WAITING : label}</p>
          {notReached ? (
            <button type="button" className="link unreached-toggle" aria-expanded={unreachedOpen} aria-controls="unreached-body"
                    onClick={() => setUnreachedOpen(!unreachedOpen)}>Returns not touched ({notReached.length})</button>
          ) : null}
        </div>
        <div className="strip-routes">
          <div className="routes-bar" aria-hidden="true">
            {reached === 0 ? <span className="empty-bar" /> : routes.map((key) => (
              <span key={key} className={`route-${key}`} style={{ width: `${((kpis?.routes[key] ?? 0) / reached) * 100}%` }} />
            ))}
          </div>
          {kpis ? (
            <ul className="routes-legend" aria-label="Routes">
              {routes.length ? routes.map((key) => (
                <li key={key}><span className={`sw route-${key}`} /><span>{ROUTE_LABELS[key]}</span><b className="n num">{kpis.routes[key]}</b></li>
              )) : <li>No return touched</li>}
            </ul>
          ) : <p className="strip-sub">{pending ? WAITING : "Each return the change touches gets its next step."}</p>}
          <p className="parity-line">
            {kpis && checked && kpis.reached > 0 ? (
              <>
                {crossCheck.label}: {checked.agree} {checked.agree === 1 ? "agrees" : "agree"}
                {checked.differs ? <>, {checked.differs} {checked.differs === 1 ? "differs" : "differ"}
                  {differClients.length ? <> (<ClientList clients={differClients} onClient={onClient} />)</> : null}</> : null}
                {checked.not_checked ? `, ${checked.not_checked} not checked` : ""}.
                {crossCheck.note ? <span className="secondary"> {crossCheck.note}</span> : null}
              </>
            ) : kpis ? `${crossCheck.label}: the change touches no return, so there is none to check.`
              : "PolicyEngine-US, an open-source tax model, redoes the math for each return the change touches."}
          </p>
        </div>
        {book.length ? <BookMap book={book} arrive={arrive} onCell={onClient} /> : null}
      </div>
      {notReached ? <NotReached rows={notReached} total={kpis?.total} unexpected={kpis?.unexpected} onRow={onClient} hidden={!unreachedOpen} /> : null}
    </section>
  );
}

// The impact list: a worklist by route ----------------------------------------------------------------------

/** The book before any change runs: every return as prepared, each opening its drawer. */
function BookTable({ book, onRow }: { book: BookRow[]; onRow: (client: string) => void }) {
  return (
    <div className="table-scroll" style={{ marginTop: 6 }}>
      <table className="data book-table">
        <thead>
          <tr>
            <th scope="col">Return</th>
            <th scope="col" className="num" title="Adjusted gross income, Form 1040 line 11">Income (AGI)</th>
            <th scope="col" className="num hide-phone" title="Schedule 1-A deductions, new for 2025">Tips, overtime, senior</th>
            <th scope="col" className="num" title="Form 1040 line 16">Tax</th>
            <th scope="col" className="num hide-phone">Tax as filed</th>
          </tr>
        </thead>
        <tbody>
          {book.map((row) => (
            <tr key={row.client} onClick={() => onRow(row.client)}>
              <td>
                <button type="button" className="row-open cell-id id" aria-label={`Open return ${row.client}`}
                        onClick={(event) => { event.stopPropagation(); onRow(row.client); }}>{row.client}</button>
                <Filing status={row.status} filing={row.filing} />
              </td>
              <td className="num"><span className="cell-label">Income </span>{amountText(row.agi)}</td>
              <td className="num hide-phone">{amountText(row.sch1a_total)}</td>
              <td className="num"><span className="cell-label">Tax </span>{amountText(row.tax)}</td>
              <td className="num hide-phone">{row.as_filed_tax ? amountText(row.as_filed_tax) : <span className="dash">-</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What the row must not hide: a document that contradicts the return as filed, and a cross-check difference
 * with its reason. */
function RowFlags({ item }: { item: ImpactRow }) {
  const badge = item.parity ? differsBadge(item.parity, item) : null;
  return (
    <>
      {item.conflict ? <span className="flag conflict"><b>Conflict:</b> <Ids text={item.conflict} /></span> : null}
      {badge ? (
        <span className="flag xcheck">
          <span className="xbadge" title={badge.title || undefined}>{badge.text}</span>
          {item.parity?.reason ? <span className="flag-why"><Ids text={capFirst(item.parity.reason)} /></span> : null}
        </span>
      ) : null}
    </>
  );
}

/** Sign-off after a change is applied: the name typed once in the list's header, and each return's decision. */
export interface SignControls {
  changeId: string;
  signer: string;
  onSigner: (name: string) => void;
  latest: Map<string, SignOff>;
  onSign: (row: ImpactRow, decision: SignDecision) => void;
}

function SignCell({ item, sign, editing, onEdit }: { item: ImpactRow; sign: SignControls; editing: boolean; onEdit: (client: string | null) => void }) {
  const role = roleFor(item.route);
  if (!role) return <td className="signoff-cell"><span className="dash">No sign-off: nothing changed</span></td>;
  const latest = sign.latest.get(item.client);
  const blank = !sign.signer.trim();
  return (
    // The cell keeps its clicks: a click on a sign-off never opens the return.
    <td className="signoff-cell" onClick={(event) => event.stopPropagation()}>
      <span className="signoff-role">{controlLabel(item.route)}</span>
      {latest && !editing ? (
        <span className="signoff-done">
          <b>{DECISION_WORDS[latest.decision]}</b> by {latest.name}, {latest.role}{clockOf(latest.at) ? ` at ${clockOf(latest.at)}` : ""}
          {" "}<button type="button" className="link" aria-label={`Change the sign-off for ${item.client}`} onClick={() => onEdit(item.client)}>Change</button>
        </span>
      ) : (
        <span className="signoff-buttons">
          {choicesFor(item.route).map((choice) => (
            <button key={choice.decision} type="button" className="btn small" disabled={blank} aria-label={`${choice.label}, ${item.client}`}
                    aria-describedby={blank ? "signer-note" : undefined}
                    onClick={() => { sign.onSign(item, choice.decision); onEdit(null); }}>{choice.button}</button>
          ))}
          {latest ? <button type="button" className="link" aria-label={`Cancel, keep the sign-off for ${item.client}`} onClick={() => onEdit(null)}>Cancel</button> : null}
        </span>
      )}
    </td>
  );
}

/** A moved line's before and after values: the after value (with the change) is its own element, so it can arrive
 * after the before value. The text is the API's. */
function BeforeAfter({ line }: { line: ImpactLine }) {
  return <>{amountText(line.before)} to <span className="now">{amountText(line.after)}{line.delta ? ` (${minus(line.delta.text)})` : ""}</span></>;
}

/** The impact list. When a result arrives (arrive, with each item's delay), its route groups and rows enter in order,
 * the after values follow their before values and each tax change settles in; then the list stays still. */
export function ImpactPanel({ rows, label, selected, pending, changeId, onRow, empty, book, sign, arrive }: {
  rows: ImpactRow[] | undefined; label: string; selected: string | null; pending: boolean; changeId?: string;
  onRow: (client: string) => void; empty: string; book?: BookRow[]; sign?: SignControls; arrive?: Arrival | null;
}) {
  const table = useRef<HTMLTableElement>(null);
  const phase = useArrival(table, arrive?.result);
  const delay = (key: string) => at(arrive?.delays.get(key));
  const [quietOpen, setQuietOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const groups = useMemo(() => groupByRoute(rows ?? []), [rows]);
  const shared = useMemo(() => sharedAuthority(rows ?? []), [rows]);
  const sharedText = shared.cite?.text;
  // Each row cites the shared authority unless it names another; only then does the table get the column.
  const own = useMemo(() => new Map((rows ?? []).map((row) => [row.client, ownAuthorities(row, sharedText)])), [rows, sharedText]);
  const differs = !shared.every && [...own.values()].some((list) => list.length > 0);
  const has = !!rows && rows.length > 0;
  const signing = has && !pending && !!sign;
  const columns = 3 + (differs ? 1 : 0) + (signing ? 1 : 0);
  const counted = signing && rows && sign ? progress(rows, sign.latest) : null;

  const exportCsv = () => {
    if (!rows) return;
    saveFile(`taxdiff-impact-list-${changeId ?? "run"}.csv`, `﻿${worklistCsv(rows, sign?.latest)}`, "text/csv;charset=utf-8");
  };

  const row = (item: ImpactRow) => {
    const line = primaryLine(item);
    const method = item.method && line && /overtime/.test(line.line) ? item.method : "";
    const cites = own.get(item.client) ?? [];
    const waits = waitsForDocument(item) && item.route !== "document";
    return (
      // The row opens the return on a click; the button in its first cell does the same by keyboard.
      <tr key={item.client} data-selected={selected === item.client} onClick={() => onRow(item.client)} style={delay(item.client)}>
        <td>
          <button type="button" className="row-open cell-id id" aria-label={`Open return ${item.client}`}
                  onClick={(event) => { event.stopPropagation(); onRow(item.client); }}>{item.client}</button>
          <Filing status={item.status} filing={item.filing} />
          {waits ? <span className="cell-sub waits">Waiting for a document</span> : null}
          {item.route_reason ? <span className="cell-sub route-reason"><Ids text={capFirst(item.route_reason)} /></span> : null}
        </td>
        <td className="hide-phone">
          {line ? (
            <>
              <span className="line-cell"><span>{lineName(line.label)}</span><LineNo no={line.no} /></span>
              <span className="cell-sub"><BeforeAfter line={line} /></span>
              {method ? <span className="method">{method}</span> : null}
            </>
          ) : <span className="dash">Nothing changed</span>}
          <RowFlags item={item} />
        </td>
        <td className="num amounts">
          {/* The column heading names the first amount on a wide screen; on a phone each amount carries its label. */}
          <span className="cell-label">Tax change </span><b className="tax-chip">{changeText(item.tax_change) === "unknown" ? <span className="dash">unknown</span> : changeText(item.tax_change)}</b>
          {item.conflict_change ? <span className="cell-sub conflict-price">Conflict: {minus(item.conflict_change.text)} under the Notice</span> : null}
          {item.tax_change.range ? <span className="cell-sub">until the document arrives</span> : null}
          {item.vs_filed ? <span className="cell-sub">vs. the filed return {changeText(item.vs_filed)}</span> : null}
        </td>
        {differs ? (
          <td className="hide-phone">
            {cites.length ? <span className="cite">{cites.map((cite, index) => <span key={cite.text}>{index ? "; " : ""}<CiteLink cite={cite} /></span>)}</span>
              : <span className="sr-only">The authority above the table</span>}
          </td>
        ) : null}
        <td className="phone-only">
          {line ? <>{lineName(line.label)} {amountText(line.before)} to <span className="now">{amountText(line.after)}</span></> : "Nothing changed"}
          {method ? <span className="method">{method}</span> : null}
          {cites.length ? <span className="method">Authority: {cites.map((cite, index) => <span key={cite.text}>{index ? "; " : ""}<CiteLink cite={cite} /></span>)}</span> : null}
          <RowFlags item={item} />
        </td>
        {signing && sign ? <SignCell item={item} sign={sign} editing={editing === item.client} onEdit={setEditing} /> : null}
      </tr>
    );
  };

  return (
    <section className="panel" aria-labelledby="impact-title" aria-busy={pending || undefined}>
      <div className="panel-head">
        <h2 id="impact-title">Impact list</h2>
        <span className="end">
          {counted ? (
            <span className="run-status signoff-progress">
              <b>Signed off {counted.done} of {counted.total}{rows && rows.length > counted.total ? " that need it" : ""}</b>
              {/* The same count as a bar that fills as returns are signed off; the words above are what is read out. */}
              <span className="signoff-bar" aria-hidden="true">
                <span style={{ transform: `scaleX(${counted.total ? counted.done / counted.total : 0})` }} />
              </span>
            </span>
          ) : null}
          <span className="run-status">{pending ? "Waiting for the run" : rows ? `${plural(rows.length, "return")} · ${label}` : label}</span>
          {has && !pending ? <button type="button" className="btn small" onClick={exportCsv}>Export CSV</button> : null}
        </span>
      </div>
      {signing && sign ? (
        <div className="signer">
          <label htmlFor="signer">Signing as</label>
          <input id="signer" type="text" autoComplete="name" maxLength={80} value={sign.signer} placeholder="Your name"
                 aria-describedby="signer-note" onChange={(event) => sign.onSigner(event.target.value)} />
          <p className="signer-note" id="signer-note">
            Names are typed, not verified; each sign-off goes in the audit log.
          </p>
        </div>
      ) : null}
      {has && shared.cite ? (
        <p className="panel-sub authority">
          {shared.every ? "Authority for every line below: " : "Authority for every line below unless the row names another: "}
          <CiteLink cite={shared.cite} />
        </p>
      ) : null}
      {!has && !pending && book?.length ? (
        <>
          <p className="panel-sub">The {book.length} sample returns before any change. {empty}</p>
          <BookTable book={book} onRow={onRow} />
        </>
      ) : !has ? (
        <div className="panel-body"><div className={`empty${pending ? " loading" : ""}`}>{pending ? WAITING : empty}</div></div>
      ) : (
        <div className="table-scroll" style={{ marginTop: 6 }}>
          <table className="data worklist" ref={table} data-signing={signing || undefined} data-arrive={phase}>
            <thead>
              <tr>
                <th scope="col">Return</th>
                <th scope="col">Line that moved: before to after</th>
                <th scope="col" className="num">Tax change</th>
                {differs ? <th scope="col">Authority</th> : null}
                {signing ? <th scope="col">Sign-off</th> : null}
              </tr>
            </thead>
            {groups.map((group) => group.route === "no_change" ? (
              // Reached, but nothing changed: one row says so, and the returns open on request.
              <tbody key={group.route}>
                <tr className="group quiet" style={delay(`group:${group.route}`)}>
                  <th scope="rowgroup" colSpan={columns}>
                    <span className={`sw route-${group.route}`} aria-hidden="true" />
                    <span className="group-name">{group.label}</span>
                    <span className="group-note">{group.rows.length === 1 ? "1 reached return: nothing changed" : `${group.rows.length} reached returns: nothing changed`}</span>
                    <button type="button" className="link" aria-expanded={quietOpen} onClick={() => setQuietOpen(!quietOpen)}>
                      {quietOpen ? "Hide" : group.rows.length === 1 ? "Show it" : "Show them"}
                    </button>
                  </th>
                </tr>
                {quietOpen ? group.rows.map(row) : null}
              </tbody>
            ) : (
              <tbody key={group.route}>
                <tr className="group" style={delay(`group:${group.route}`)}>
                  <th scope="rowgroup" colSpan={columns}>
                    <span className={`sw route-${group.route}`} aria-hidden="true" />
                    <span className="group-name">{group.label}</span>
                    <span className="group-count num">{group.rows.length}</span>
                  </th>
                </tr>
                {group.rows.map(row)}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </section>
  );
}

// The answer --------------------------------------------------------------------------------------------------

/** Every line label the result carries (the asked line, the chains, the impact lines), by line key. */
function lineLabels(result: RunResult | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (node: ChainNode | undefined) => {
    if (!node) return;
    out[node.line] ??= node.label;
    node.uses.forEach(walk);
  };
  if (result?.line && result.line_label) out[result.line] = result.line_label;
  walk(result?.chain);
  for (const row of result?.impact ?? []) {
    for (const line of row.lines) {
      out[line.line] ??= line.label;
      walk(line.chain);
    }
  }
  return out;
}

export function AnswerText({ text, links, labels, onFigure, className = "answer-text" }: {
  text: string; links: (Link | null)[]; labels: Record<string, string>; onFigure: (link: Link) => void; className?: string;
}) {
  const parts: React.ReactNode[] = [];
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(FIGURE)) {
    let start = match.index ?? 0;
    let shown = match[0];
    // The figure pattern reads the "-021" of "C-021" as a figure; show the whole id as one token.
    const prefix = /\b(CS|C)$/.exec(text.slice(Math.max(last, start - 2), start));
    if (prefix && shown.startsWith("-")) {
      start -= prefix[1].length;
      shown = prefix[1] + shown;
    }
    if (start > last) parts.push(<Ids key={`t${last}`} text={text.slice(last, start)} />);
    const link = links[index];
    const label = link?.label ?? (link?.line ? labels[link.line] : undefined);
    parts.push(link?.client
      ? <button key={start} type="button" className="fig num" onClick={() => onFigure(link)} title={`Show ${link.client}${label ? `, ${lowerFirst(lineName(label))}` : ""}`}>{prefix ? shown : minus(shown)}</button>
      : <span key={start} className={prefix ? "id" : undefined}>{prefix ? shown : minus(shown)}</span>);
    last = (match.index ?? 0) + match[0].length;
    index += 1;
  }
  if (last < text.length) parts.push(<Ids key={`t${last}`} text={text.slice(last)} />);
  return <p className={className}>{parts}</p>;
}

export function ChainView({ node }: { node: ChainNode }) {
  return (
    <ul className="chain">
      <ChainItem node={node} />
    </ul>
  );
}

function readText(read: ChainRead): string {
  if (read.kind === "rule") return `${read.label ?? read.name}: ${read.value}${read.cite ? ` (${read.cite})` : ""}${read.set_by ? `, set by ${read.set_by.change}` : ""}`;
  if (read.kind === "fact") {
    const from = read.source?.change ? `${read.source.change}${read.source.document ? ", employer statement" : ""}` : read.source?.text ?? "";
    return `${read.label ?? read.name}: ${read.value}${from ? ` (${from})` : ""}`;
  }
  return `${read.summary || read.name}${read.version ? ` (version ${read.version})` : ""}`;
}

function ChainItem({ node }: { node: ChainNode }) {
  return (
    <li>
      <div className="ln"><span>{lineName(node.label)}</span><LineNo no={node.no} /><span className="v num">{minus(node.value.text)}</span></div>
      {node.reads.length ? (
        <ul className="reads">
          {node.reads.map((read) => (
            <li key={`${read.kind}-${read.name}`} data-touched={read.touched} title={read.name}>
              {read.touched ? <span className="edited">edited</span> : null}
              <span className="kind">{read.kind}</span>{readText(read)}
            </li>
          ))}
        </ul>
      ) : null}
      {node.uses.length ? <ul className="chain uses">{node.uses.map((child) => <ChainItem key={child.line} node={child} />)}</ul> : null}
    </li>
  );
}

function answerFoot(source: string, wordedBy: string): string {
  if (isWorded(source)) return `Worded by ${wordedBy}; code checked every figure, client and change ID against the run.`;
  const reason = templateReason(source);
  return `From a template; every figure comes from the run.${reason ? ` The model's wording was not used: ${reason}.` : ""}`;
}

function PartsView({ parts, labels, onFigure, onAsk }: { parts: AnswerPart[]; labels: Record<string, string>; onFigure: (link: Link) => void; onAsk: (question: string) => void }) {
  return (
    <>
      {parts.map((part, index) => {
        const target = part.target;
        if (target?.question) {
          const question = target.question;
          return <button key={index} type="button" className="link try" onClick={() => onAsk(question)}>{part.text}</button>;
        }
        if (target?.client) {
          const label = target.label ?? (target.line ? labels[target.line] : undefined);
          return (
            <button key={index} type="button" className="fig num" onClick={() => onFigure(target)}
                    title={`Show ${target.client}${label ? `, ${lowerFirst(lineName(label))}` : ""}`}>{minus(part.text)}</button>
          );
        }
        return <span key={index}><Ids text={part.text} /></span>;
      })}
    </>
  );
}

function SectionView({ section, labels, onFigure, onAsk }: { section: AnswerSection; labels: Record<string, string>; onFigure: (link: Link) => void; onAsk: (question: string) => void }) {
  if (section.type === "paragraph") return <p className="answer-para"><PartsView parts={section.content} labels={labels} onFigure={onFigure} onAsk={onAsk} /></p>;
  if (section.type === "facts") {
    return (
      <div className="answer-block">
        {section.title ? <h3>{section.title}</h3> : null}
        <dl className="answer-facts">
          {section.rows.map((row, index) => (
            <div key={index} className="answer-fact">
              <dt>{row.label}</dt>
              <dd className="num"><PartsView parts={row.value} labels={labels} onFigure={onFigure} onAsk={onAsk} /></dd>
            </div>
          ))}
        </dl>
      </div>
    );
  }
  return (
    <div className="answer-block">
      {section.title ? <h3>{section.title}</h3> : null}
      <ul className="answer-list">
        {section.items.map((item, index) => <li key={index}><PartsView parts={item} labels={labels} onFigure={onFigure} onAsk={onAsk} /></li>)}
      </ul>
    </div>
  );
}

/** The answer to a run. After a decision (`brief`) it shows its headline and folds the rest, so the worklist comes
 * right after the decision line; the rest stays in the page and opens on request. */
export function AnswerCard({ result, question, pending, wordedBy, onFigure, onAsk, onRerun, onReset, brief = false }: {
  result: RunResult | undefined; question?: string; pending: boolean; wordedBy: string;
  onFigure: (link: Link) => void; onAsk: (question: string) => void; onRerun: () => void; onReset: () => void; brief?: boolean;
}) {
  const answer = result?.answer;
  const labels = useMemo(() => lineLabels(result), [result]);
  const [opened, setOpened] = useState<RunResult | null>(null);
  const full = !brief || (!!result && opened === result);
  return (
    <section className="panel answer-card" aria-labelledby="answer-title" aria-busy={pending || undefined} data-kind={answer?.kind ?? ""}>
      <div className="panel-head"><h2 id="answer-title" tabIndex={-1}>Answer</h2></div>
      {question && result ? <p className="panel-sub run-question" title={question}>Question: {question}</p> : null}
      <div className="panel-body">
        {!result || (pending && !answer && !result.message)
          ? <div className={`empty${pending ? " loading" : ""}`}>{pending ? WAITING : "Ask about a client or a change, or pick an example. The answer is written from the run, and every figure in it links to its return."}</div>
          : !answer && result.message ? (
            <div className="notice error" role="alert">
              {capFirst(result.message)}
              <div style={{ marginTop: 8 }}>
                {result.history_error || result.status === "history_full"
                  ? <button type="button" className="btn small" onClick={onReset}>Reset the session</button>
                  : <button type="button" className="btn small" onClick={onRerun}>Run the question again</button>}
              </div>
            </div>
          )
          : !answer ? <div className="empty">No answer for this run.</div>
          : (
            <>
              {answer.headline !== undefined && answer.sections ? (
                <>
                  <AnswerText text={answer.headline} links={answer.headline_links ?? []} labels={labels} onFigure={onFigure} className="answer-headline" />
                  {brief && answer.sections.length ? (
                    <button type="button" className="link answer-more" aria-expanded={full} aria-controls="answer-sections"
                            onClick={() => setOpened(full ? null : result ?? null)}>{full ? "Show less" : "Show the full answer"}</button>
                  ) : null}
                  <div className="answer-sections" id="answer-sections" hidden={!full}>
                    {answer.sections.map((section, index) => <SectionView key={index} section={section} labels={labels} onFigure={onFigure} onAsk={onAsk} />)}
                  </div>
                </>
              ) : <AnswerText text={answer.text} links={answer.links} labels={labels} onFigure={onFigure} />}
              {result.kind !== "unsupported" ? <div className="answer-foot">{answerFoot(answer.source, wordedBy)}</div> : null}
              {result.kind === "explain" && result.chain ? (
                <details className="inputs">
                  <summary>Inputs to {result.client}&apos;s {lowerFirst(lineName(result.line_label ?? "line"))}</summary>
                  <div className="inputs-body"><ChainView node={result.chain} /></div>
                  {result.parity ? <p className="answer-foot">PolicyEngine-US: {PARITY_LABELS[result.parity.state]}{result.parity.reason ? ` (${result.parity.reason})` : ""}</p> : null}
                </details>
              ) : null}
            </>
          )}
      </div>
    </section>
  );
}
