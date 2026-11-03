// The tab's audit log: the events this tab records itself (sign-offs, undo, reset, a materiality change) and
// the CSV download. The API's events are kept as it sent them.

import { csvCell } from "./worklist";
import { DECISION_WORDS, controlLabel, type SignOff } from "./signoff";
import type { AuditEvent, PackView } from "./types";

/** Local time in the API's audit format: "2026-11-02T10:42:07". */
export function localIso(date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** "$50" for a materiality the session keeps as "50". */
export function dollars(materiality: string | undefined): string {
  if (!materiality) return "";
  if (materiality.startsWith("$")) return materiality;
  const n = Number(materiality);
  return Number.isFinite(n) ? `$${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}` : `$${materiality}`;
}

interface Base { title: string; kind: string; pack?: PackView; materiality?: string }

/** The API stamps audit events with the server's clock and no zone; the run's start carries the same moment in
 * UTC. Their difference, rounded to a quarter hour, is the server's offset, so each event gets its UTC time and
 * the log can show the viewer's clock, the same one the decision line uses. */
export function withUtc(events: AuditEvent[], startedUtc: string | undefined): AuditEvent[] {
  const started = startedUtc ? Date.parse(startedUtc) : Number.NaN;
  if (Number.isNaN(started)) return events;
  return events.map((event) => {
    if (event.at_utc) return event;
    const naive = Date.parse(`${event.at.slice(0, 19)}Z`);
    if (Number.isNaN(naive)) return event;
    const offset = Math.round((naive - started) / 900_000) * 900_000;
    return { ...event, at_utc: new Date(naive - offset).toISOString() };
  });
}

/** "2026-11-02 19:43:25" on the viewer's clock, or the server's own text when the moment is not known. */
function localStamp(event: AuditEvent): string {
  if (!event.at_utc) return event.at.replace("T", " ").slice(0, 19);
  return localIso(new Date(event.at_utc)).replace("T", " ");
}

function local(fields: Partial<AuditEvent> & Pick<AuditEvent, "change_id" | "outcome" | "detail">, base: Base): AuditEvent {
  const now = new Date();
  return {
    at: localIso(now),
    at_utc: now.toISOString(),
    title: base.title,
    kind: base.kind,
    author: "",
    pack_version: base.pack?.version ?? 1,
    approver: "",
    pack_sha: base.pack?.sha ?? "",
    facts: "",
    materiality: dollars(base.materiality),
    parity: "",
    engine: "",
    local: true,
    ...fields,
  };
}

export function signOffEvent(item: SignOff, base: Base): AuditEvent {
  const decision = DECISION_WORDS[item.decision];
  return local({
    change_id: item.change_id, outcome: "signed off", client: item.client, role: item.role, decision, approver: item.name,
    detail: `${item.client}: ${decision} (${controlLabel(item.route).toLowerCase()})`,
  }, { ...base, kind: "sign-off" });
}

export function undoEvent(changeId: string, detail: string, name: string, base: Base): AuditEvent {
  return local({ change_id: changeId, outcome: "undone", detail, approver: name }, { ...base, kind: "undo" });
}

export function resetEvent(detail: string, name: string, base: Omit<Base, "title" | "kind">): AuditEvent {
  return local({ change_id: "-", outcome: "session reset", detail, approver: name }, { ...base, title: "Reset", kind: "session" });
}

export function materialityEvent(from: string, to: string, name: string, pack?: PackView): AuditEvent {
  return local({
    change_id: "-", outcome: "amend threshold changed", approver: name,
    detail: `firm setting (materiality) ${dollars(from)} to ${dollars(to)}; it applies from the next preview`,
  }, { title: "Firm setting", kind: "setting", pack, materiality: to });
}

const HEAD = ["time (your clock)", "server time", "change", "title", "kind", "outcome", "client", "decision", "name", "role",
  "amend threshold", "rules version", "rule pack hash", "facts", "cross-check", "engine", "detail", "recorded by"];

/** The audit log as CSV, oldest first, every value as recorded. */
export function auditCsv(events: AuditEvent[]): string {
  const out = [HEAD.join(",")];
  for (const event of events) {
    out.push([
      localStamp(event), event.at, event.change_id, event.title, event.kind, event.outcome, event.client ?? "", event.decision ?? "",
      event.approver, event.role ?? "", event.materiality, `v${event.pack_version}`, event.pack_sha, event.facts,
      event.parity, event.engine, event.detail, event.local ? "this tab" : "server",
    ].map((cell) => csvCell(String(cell ?? ""))).join(","));
  }
  return `${out.join("\r\n")}\r\n`;
}
