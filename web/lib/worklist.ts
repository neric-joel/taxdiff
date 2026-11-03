// The impact list as a worklist: rows grouped by route, the authority said once, the counts of what approving
// writes, and the CSV export. Every amount is the API's text; nothing here computes one.

import { DECISION_WORDS, type SignOff } from "./signoff";
import { FILING_LABELS, ROUTE_LABELS, ROUTE_ORDER, STATUS_LABELS } from "./format";
import type { Amount, ImpactLine, ImpactRow, Note, Parity, RouteKey, WorksheetStep } from "./types";

/** The line a row leads with: the one the API names, else the first line that moved other than wages and AGI. */
export function primaryLine(row: ImpactRow): ImpactLine | undefined {
  const named = row.primary_line ? row.lines.find((line) => line.line === row.primary_line) : undefined;
  return named ?? row.lines.find((line) => !["wages", "agi"].includes(line.line)) ?? row.lines[0];
}

/** A return waits for a document when the API lists something it needs, whatever its route. */
export function waitsForDocument(row: ImpactRow): boolean {
  return row.needs.length > 0 || row.route === "document";
}

/** The return gets a letter to the client: a document request, or the notice a filed return gets when it goes
 * to amendment review or is below materiality. */
export function writesClientLetter(row: ImpactRow): boolean {
  return waitsForDocument(row) || (row.status === "filed" && (row.route === "amendment" || row.route === "below_materiality"));
}

/** What approving writes: an internal note for every reached return, and the letters to clients (the preview's
 * own list when the API sends one). */
export function noteCounts(rows: ImpactRow[], letters?: Note[]): { internal: number; letters: number } {
  return {
    internal: rows.length,
    letters: letters ? letters.length : rows.filter(writesClientLetter).length,
  };
}

/** A note written to the client rather than for the firm. */
export function isLetter(note: Note): boolean {
  return note.kind === "client_notice" || note.kind === "document_request";
}

export const LETTER_KIND: Record<string, string> = { client_notice: "notice", document_request: "document request" };

/** Where a line sits, from the row's own lines or their inputs: "1-A 21". */
export function lineNoOf(row: ImpactRow, key: string): string {
  for (const line of row.lines) {
    if (line.line === key) return line.no;
    const stack = [line.chain];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      if (node.line === key) return node.no;
      stack.push(...node.uses);
    }
  }
  return "";
}

/** "line 21" for "1-A 21", "line 16" for "16". */
export function shortLine(no: string): string {
  return no ? `line ${no.replace(/^1-A /, "")}` : "";
}

/** The badge a row shows when the cross-check differs: "Cross-check differs by $50 (line 21)". The difference
 * is shown only when the API states it; the screen does not subtract. */
export function differsBadge(parity: Parity, row?: ImpactRow): { text: string; title: string } | null {
  if (parity.state !== "differs") return null;
  const first = parity.diffs[0];
  if (!first) return { text: "Cross-check differs", title: "" };
  const no = first.no || (row ? lineNoOf(row, first.line) : "");
  const where = shortLine(no) || first.label.split(",")[0].toLowerCase();
  const more = parity.diffs.length > 1 ? ` and ${parity.diffs.length - 1} more` : "";
  return {
    text: `Cross-check differs${first.difference ? ` by ${first.difference}` : ""} (${where}${more})`,
    title: parity.diffs.map((d) => `${d.label}: this project's engine ${d.engine_a}, PolicyEngine-US ${d.policyengine}`).join("\n"),
  };
}

export interface RouteGroup { route: RouteKey; label: string; rows: ImpactRow[] }

/** The rows by route, most urgent first, in the API's order inside each route. */
export function groupByRoute(rows: ImpactRow[]): RouteGroup[] {
  const extra = [...new Set(rows.map((row) => row.route).filter((route) => !ROUTE_ORDER.includes(route)))];
  return [...ROUTE_ORDER, ...extra]
    .map((route) => {
      const members = rows.filter((row) => row.route === route);
      return { route, label: ROUTE_LABELS[route] ?? members[0]?.route_label ?? route, rows: members };
    })
    .filter((group) => group.rows.length > 0);
}

export interface Cite { text: string; url: string | null }

/** The authority most changed lines cite, to say once above the table, and whether every line cites it. */
export function sharedAuthority(rows: ImpactRow[]): { cite: Cite | null; every: boolean } {
  const seen = new Map<string, { cite: Cite; n: number }>();
  for (const row of rows) {
    for (const line of row.lines) {
      const found = seen.get(line.citation.text);
      if (found) found.n += 1;
      else seen.set(line.citation.text, { cite: { text: line.citation.text, url: line.citation.url || null }, n: 1 });
    }
  }
  const top = [...seen.values()].sort((a, b) => b.n - a.n)[0];
  return { cite: top?.cite ?? null, every: seen.size === 1 };
}

/** The authorities a row's lines cite other than the shared one. */
export function ownAuthorities(row: ImpactRow, shared: string | undefined): Cite[] {
  const out = new Map<string, Cite>();
  for (const line of row.lines) {
    if (line.citation.text !== shared && !out.has(line.citation.text)) out.set(line.citation.text, { text: line.citation.text, url: line.citation.url || null });
  }
  return [...out.values()];
}

const CSV_HEAD = ["client", "status", "filing status", "route", "route reason", "line", "before", "after", "tax change", "against as filed",
  "authority", "method", "conflict", "cross-check", "cross-check reason", "sign-off", "signed by", "role", "signed at"];

// A spreadsheet runs a cell that starts with = + - or @ as a formula, so such a cell is written as text unless
// it is a plain number.
export function csvCell(value: string): string {
  const cell = /^[=+\-@\t\r]/.test(value) && !/^-?\d+(\.\d+)?$/.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell;
}

/** An amount as a plain number for a spreadsheet, or the API's text when there is no single value (a range). */
function amountCell(amount: Amount | null | undefined): string {
  if (!amount) return "";
  return amount.value !== null ? amount.value : amount.text;
}

/** The impact list as CSV: one row per return, in the list's order, each value as the API gave it, with the
 * latest sign-off of each return when there is one. */
export function worklistCsv(rows: ImpactRow[], signed?: Map<string, SignOff>): string {
  const out = [CSV_HEAD.join(",")];
  for (const group of groupByRoute(rows)) {
    for (const row of group.rows) {
      const line = primaryLine(row);
      const sign = signed?.get(row.client);
      out.push([
        row.client,
        STATUS_LABELS[row.status] ?? row.status,
        FILING_LABELS[row.filing] ?? row.filing,
        row.route_label || group.label,
        row.route_reason ?? "",
        line?.label ?? "",
        amountCell(line?.before),
        amountCell(line?.after),
        amountCell(row.tax_change),
        amountCell(row.vs_filed),
        line?.citation.text ?? "",
        row.method ?? "",
        row.conflict ?? "",
        row.parity?.state.replace("_", " ") ?? "",
        row.parity?.reason ?? "",
        sign ? DECISION_WORDS[sign.decision] : "",
        sign?.name ?? "",
        sign?.role ?? "",
        sign?.at ?? "",
      ].map(csvCell).join(","));
    }
  }
  return `${out.join("\r\n")}\r\n`;
}

// label is the step's wording after the change; beforeLabel is set when the step read differently before it.
export interface SheetRow { key: string; ref: string; label: string; beforeLabel?: string; before: string | null; after: string | null }

function keyed(steps: WorksheetStep[]): { key: string; step: WorksheetStep }[] {
  const seen = new Map<string, number>();
  return steps.map((step) => {
    const base = step.line || step.label;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return { key: n ? `${base}#${n}` : base, step };
  });
}

/** The worksheet's before and after steps side by side, matched by line, in the order they run. A step only
 * one side has keeps its place after the step before it. */
export function worksheetRows(before: WorksheetStep[], after: WorksheetStep[]): SheetRow[] {
  const was = keyed(before);
  const now = keyed(after);
  const order: string[] = [];
  for (const list of [was, now]) {
    let at = 0;
    for (const { key } of list) {
      const index = order.indexOf(key);
      if (index >= 0) at = index + 1;
      else order.splice(at++, 0, key);
    }
  }
  const wasBy = new Map(was.map(({ key, step }) => [key, step]));
  const nowBy = new Map(now.map(({ key, step }) => [key, step]));
  return order.map((key) => {
    const was = wasBy.get(key);
    const step = (nowBy.get(key) ?? was) as WorksheetStep;
    const beforeLabel = was && was.label !== step.label ? was.label : undefined;
    return { key, ref: step.ref, label: step.label, beforeLabel, before: was?.value ?? null, after: nowBy.get(key)?.value ?? null };
  });
}
