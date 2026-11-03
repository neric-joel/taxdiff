// Browser session state, kept in sessionStorage for this tab only. Two keys: the core (approved change sets
// with their receipts, the state before each one so the last can be undone, the sign-offs, the audit log, the
// current rule pack, facts and book), which must survive, and the run history, which is trimmed first when
// storage is short. Every access is guarded, since storage can be blocked.

import type { SignOff } from "./signoff";
import type { AuditEvent, BookRow, HistoryItem, PackView, RunRecord, RunResult } from "./types";

const CORE = "taxdiff:core:v2";
const RUNS = "taxdiff:runs:v2";

/** The session as it stood before an approved change: what Undo restores. */
export interface Checkpoint {
  change_id: string;
  title: string;
  approver: string;
  at: string; // when the change was applied
  pack?: PackView;
  facts?: Record<string, number>;
  book?: BookRow[];
}

export interface SessionState {
  history: HistoryItem[];
  checkpoints: Checkpoint[]; // one per approved change, in the same order
  signoffs: SignOff[];
  signer: string; // the "Signing as" name, typed, not verified
  runs: RunRecord[];
  audit: AuditEvent[];
  materiality: string; // the firm setting, in dollars
  pack?: PackView; // the rule pack after the last approved change
  facts?: Record<string, number>; // facts versions after the last approved change
  currentBook?: BookRow[]; // the book as the last run that read it saw it (never a preview or a what-if)
}

export const emptySession: SessionState = { history: [], checkpoints: [], signoffs: [], signer: "", runs: [], audit: [], materiality: "50" };

/** A run as stored: its events only. The preview and the result are read back from the events. */
function rebuild(run: RunRecord): RunRecord {
  const results = run.events.filter((e): e is { type: "result"; run_id: string; t: number; result: RunResult } => e.type === "result");
  return {
    ...run,
    result: results.length ? results[results.length - 1].result : undefined,
    preview: results.find((e) => e.result.status === "waiting")?.result,
  };
}

export function loadSession(): SessionState {
  try {
    const core = JSON.parse(window.sessionStorage.getItem(CORE) ?? "null") as Partial<SessionState> | null;
    const runs = JSON.parse(window.sessionStorage.getItem(RUNS) ?? "[]") as RunRecord[];
    if (!core) return emptySession;
    const history = Array.isArray(core.history) ? core.history : [];
    const checkpoints = Array.isArray(core.checkpoints) ? core.checkpoints : [];
    return {
      history,
      // A session saved before checkpoints were kept has none; its changes cannot be undone one at a time.
      checkpoints: checkpoints.length === history.length ? checkpoints : [],
      signoffs: Array.isArray(core.signoffs) ? core.signoffs : [],
      signer: typeof core.signer === "string" ? core.signer : "",
      audit: Array.isArray(core.audit) ? core.audit : [],
      materiality: typeof core.materiality === "string" ? core.materiality : "50",
      pack: core.pack,
      facts: core.facts,
      currentBook: core.currentBook,
      runs: Array.isArray(runs) ? runs.map(rebuild) : [],
    };
  } catch {
    return emptySession;
  }
}

/** Save the session. Returns false when even the core (approvals and audit log) could not be saved. */
export function saveSession(state: SessionState): boolean {
  try {
    const { runs, ...core } = state;
    window.sessionStorage.setItem(CORE, JSON.stringify({ ...core, audit: core.audit.slice(-400) }));
    const compact = runs.map(({ preview: _preview, result: _result, ...run }) => run);
    for (const keep of [15, 8, 3, 0]) {
      try {
        window.sessionStorage.setItem(RUNS, JSON.stringify(keep ? compact.slice(-keep) : []));
        return true;
      } catch {
        // storage is full: keep fewer past runs, never the approvals
      }
    }
    return true;
  } catch {
    return false;
  }
}

export function readFlag(name: string): string | null {
  try {
    return window.localStorage.getItem(`taxdiff:${name}`);
  } catch {
    return null;
  }
}

export function writeFlag(name: string, value: string): void {
  try {
    window.localStorage.setItem(`taxdiff:${name}`, value);
  } catch {
    // ignored: the flag only saves a click next time
  }
}
