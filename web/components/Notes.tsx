"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ROUTE_ORDER, dateOf, plural } from "@/lib/format";
import type { Note, RouteKey } from "@/lib/types";
import { LETTER_KIND, isLetter } from "@/lib/worklist";
import { Ids } from "./Ids";
import { RoutePill, WAITING, isWorded, templateReason } from "./Panels";

/** Who reworded the notes (for example "the Anthropic API (claude-haiku-5-5)"), and why the templates
 * stayed when nothing was reworded. Both come from the Write notes step of the run. */
export interface Wording { by: string; reason: string }

type Filter = "all" | "letters" | "notes";
interface ClientNotes { client: string; route: RouteKey; internal: Note[]; letters: Note[] }

/** Each client's internal note and letters, in the impact list's order: most urgent route first. */
function byClient(notes: Note[]): ClientNotes[] {
  const out = new Map<string, ClientNotes>();
  for (const note of notes) {
    const entry = out.get(note.client) ?? { client: note.client, route: note.route, internal: [], letters: [] };
    (isLetter(note) ? entry.letters : entry.internal).push(note);
    out.set(note.client, entry);
  }
  return [...out.values()].sort((a, b) => ROUTE_ORDER.indexOf(a.route) - ROUTE_ORDER.indexOf(b.route) || a.client.localeCompare(b.client));
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // no clipboard permission: fall back to a hidden selection
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

function letterName(note: Note): string {
  return `Letter to client, ${LETTER_KIND[note.kind] ?? "letter"}`;
}

/** The statement of who wrote the notes, counted from each note's own source. */
function wordingStatement(internal: Note[], letters: Note[], wording: Wording): string {
  const reworded = internal.filter((note) => isWorded(note.source)).length;
  const kept = internal.length - reworded;
  const why = wording.reason || [...new Set(internal.map((note) => templateReason(note.source)).filter(Boolean))].join("; ");
  const notes = !internal.length ? ""
    : reworded ? `${reworded} of ${plural(internal.length, "internal note")} reworded by ${wording.by}; ${kept} kept ${kept === 1 ? "its" : "their"} template. Code checked every figure, client and route in the reworded notes.`
    : `${internal.length === 1 ? "The internal note kept its" : `All ${internal.length} internal notes kept their`} template${why ? ` (${why})` : ""}; code inserted every figure, client and route.`;
  const letterWorded = letters.filter((note) => isWorded(note.source)).length;
  const sent = !letters.length ? ""
    : letterWorded ? `${letterWorded} of ${plural(letters.length, "letter")} reworded by ${wording.by}.`
    : `${letters.length === 1 ? "The letter to the client is" : `The ${letters.length} letters to clients are`} drafted by code from fixed templates and never reworded.`;
  return [notes, sent].filter(Boolean).join(" ");
}

export function NotesPanel({ notes, empty, pending, wording }: { notes: Note[] | undefined; empty: string; pending: boolean; wording: Wording }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [all, setAll] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const [printing, setPrinting] = useState<Note | null>(null);
  const copyTimer = useRef<number | null>(null);
  const internal = useMemo(() => (notes ?? []).filter((note) => !isLetter(note)), [notes]);
  const letters = useMemo(() => (notes ?? []).filter(isLetter), [notes]);
  const groups = useMemo(() => byClient(notes ?? []), [notes]);
  const visible = groups.filter((group) => filter === "all" || (filter === "letters" ? group.letters.length > 0 : group.internal.length > 0));
  const shown = all ? visible : visible.slice(0, 4);
  const reworded = internal.some((note) => isWorded(note.source));

  useEffect(() => () => { if (copyTimer.current !== null) window.clearTimeout(copyTimer.current); }, []);

  // Print one letter: the sheet below is the only thing the print stylesheet shows while body has the class.
  useEffect(() => {
    if (!printing) return;
    const done = () => {
      document.body.classList.remove("printing-letter");
      setPrinting(null);
    };
    document.body.classList.add("printing-letter");
    window.addEventListener("afterprint", done, { once: true });
    const timer = window.setTimeout(() => window.print(), 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("afterprint", done);
    };
  }, [printing]);

  const copy = async (key: string, note: Note, what: string) => {
    const ok = await copyText(note.text);
    setCopied(ok ? key : null);
    setSaid(ok ? `Copied the ${what} for ${note.client}.` : "The browser did not allow copying. Select the text instead.");
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(null), 2000);
  };

  const block = (note: Note, index: number) => {
    const letter = isLetter(note);
    const key = `${note.kind}-${note.client}-${index}`;
    const what = letter ? "letter" : "internal note";
    const kept = !letter && reworded && !isWorded(note.source);
    const reason = templateReason(note.source);
    return (
      <div key={key} className={`note-block${letter ? " letter" : ""}`}>
        <div className="note-head">
          <p className="note-title">{letter ? letterName(note) : "Internal note"}</p>
          <span className="note-actions">
            <button type="button" className="btn small" aria-label={`${copied === key ? "Copied" : "Copy"} the ${what} for ${note.client}`}
                    onClick={() => void copy(key, note, what)}>{copied === key ? "Copied" : "Copy"}</button>
            {letter ? <button type="button" className="btn small" aria-label={`Print the letter to ${note.client}`} onClick={() => setPrinting(note)}>Print</button> : null}
          </span>
        </div>
        <p className="note-text"><Ids text={note.text} /></p>
        {kept ? <div className="src">Kept its template{reason ? `: ${reason}` : ""}</div> : null}
      </div>
    );
  };

  const total = internal.length + letters.length;
  return (
    <section className="panel" aria-labelledby="notes-title" aria-busy={pending || undefined}>
      <div className="panel-head">
        <h2 id="notes-title">Notes and letters</h2>
        {notes && !pending ? <span className="end run-status">{plural(internal.length, "internal note")}, {plural(letters.length, "letter")}</span> : null}
        {pending ? <span className="end run-status">Waiting for the run</span> : null}
      </div>
      {notes && !pending ? <p className="panel-sub">{wordingStatement(internal, letters, wording)}</p> : null}
      <div className="sr-only" role="status" aria-live="polite">{said}</div>
      <div className="panel-body">
        {pending && !notes ? <div className="empty loading">{WAITING}</div> : !notes ? <div className="empty">{empty}</div> : (
          <>
            <div className="filters" role="group" aria-label="Show">
              {([["all", `All (${total})`], ["letters", `Letters (${letters.length})`], ["notes", `Notes (${internal.length})`]] as [Filter, string][]).map(([key, label]) => (
                <button key={key} type="button" className="btn small" aria-pressed={filter === key} onClick={() => { setFilter(key); setAll(false); }}>{label}</button>
              ))}
            </div>
            {visible.length === 0 ? <div className="empty">{filter === "letters" ? "This change writes no letter to a client." : "This change writes no internal note."}</div> : (
              <ul className="notes">
                {shown.map((group) => (
                  <li key={group.client}>
                    <h3 className="note-who"><span className="id">{group.client}</span><RoutePill route={group.route} /></h3>
                    {filter !== "letters" ? group.internal.map(block) : null}
                    {filter !== "notes" ? group.letters.map((note, index) => block(note, index + group.internal.length)) : null}
                  </li>
                ))}
              </ul>
            )}
            {visible.length > 4 ? (
              <button type="button" className="link" onClick={() => setAll(!all)} style={{ marginTop: 8 }}>{all ? "Show fewer" : `Show all ${visible.length} clients`}</button>
            ) : null}
          </>
        )}
      </div>
      {printing ? createPortal(
        <div className="print-sheet">
          <p className="print-mark">SYNTHETIC: a sample letter from the TaxDiff prototype. The client and the amounts are made up.</p>
          <h1>Letter to {printing.client}</h1>
          <p className="print-meta">{letterName(printing)} · {dateOf(new Date().toISOString())}</p>
          <p className="print-text">{printing.text}</p>
        </div>,
        document.body,
      ) : null}
    </section>
  );
}
