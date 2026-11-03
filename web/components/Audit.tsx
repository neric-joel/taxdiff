"use client";

import { useState } from "react";

import { auditCsv } from "@/lib/audit";
import { saveFile } from "@/lib/files";
import { MATERIALITY_HELP, plural, secondsOf } from "@/lib/format";
import type { AuditEvent } from "@/lib/types";
import { Ids } from "./Ids";

/** "J. Doe, Partner" for a sign-off; the name alone for an approval. */
function nameOf(event: AuditEvent): string {
  return event.approver ? `${event.approver}${event.role ? `, ${event.role}` : ""}` : "";
}

/** The event's time on the viewer's clock, the same clock as the decision line; the server's own text when the
 * moment is not known. */
function timeOf(event: AuditEvent): string {
  return (event.at_utc ? secondsOf(event.at_utc) : "") || event.at.slice(11, 19);
}

export function AuditPanel({ events }: { events: AuditEvent[] }) {
  const [all, setAll] = useState(false);
  const [hashes, setHashes] = useState(false);
  const newest = [...events].reverse();
  const shown = all ? newest : newest.slice(0, 6);
  const download = () => saveFile("taxdiff-audit-log.csv", `﻿${auditCsv(events)}`, "text/csv;charset=utf-8");
  return (
    <section className="panel" aria-labelledby="audit-title">
      <div className="panel-head">
        <h2 id="audit-title">Audit log</h2>
        <span className="end">
          <span className="run-status">{plural(events.length, "event")}</span>
          {events.length ? <button type="button" className="btn small" onClick={download}>Download audit log (CSV)</button> : null}
        </span>
      </div>
      <p className="panel-sub">
        Kept in this tab only. Times are on your clock.
        {events.length ? <> <button type="button" className="link" onClick={() => setHashes(!hashes)}>{hashes ? "Hide rule pack hashes" : "Show rule pack hashes"}</button></> : null}
      </p>
      {events.length === 0 ? (
        <div className="panel-body"><div className="empty">Each preview, decision, sign-off and error is recorded here with the name typed for it and the rules version it ran against.</div></div>
      ) : (
        <div className="table-scroll" style={{ marginTop: 6 }}>
          <table className="data audit">
            <colgroup>
              <col className="c-time" /><col className="c-change" /><col className="c-outcome" /><col className="c-name" />
              <col className="c-threshold" /><col className="c-rules" /><col />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">Time</th><th scope="col">Change</th><th scope="col">Outcome</th><th scope="col">Name</th>
                <th scope="col" title={`Materiality. ${MATERIALITY_HELP}`}>Amend threshold</th><th scope="col">Rules</th><th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((event, index) => (
                <tr key={`${event.at}-${event.change_id}-${event.outcome}-${index}`} data-local={event.local || undefined} style={{ cursor: "default" }}>
                  <td className="time">{timeOf(event)}</td>
                  <td><b className="id">{event.change_id}</b></td>
                  <td className="hide-phone">{event.outcome}</td>
                  <td className="hide-phone">{nameOf(event) || <span className="dash">-</span>}</td>
                  <td className="hide-phone">{event.materiality || <span className="dash">-</span>}</td>
                  <td className="hide-phone">v{event.pack_version}{hashes && event.pack_sha ? <span className="muted mono hash">{event.pack_sha}</span> : null}</td>
                  <td className="hide-phone"><span className="detail"><Ids text={event.detail} /></span></td>
                  <td className="phone-only">
                    {event.outcome}{nameOf(event) ? ` by ${nameOf(event)}` : ""} · rules v{event.pack_version}{hashes && event.pack_sha ? ` ${event.pack_sha}` : ""}
                    {event.materiality ? ` · amend threshold ${event.materiality}` : ""} · <Ids text={event.detail} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {events.length > 6 ? <div className="panel-body" style={{ paddingTop: 10 }}><button type="button" className="link" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${events.length}`}</button></div> : null}
        </div>
      )}
    </section>
  );
}
