// A run's outcome in words, for the steps strip, the history list and the screen reader.

import { capFirst, plural } from "./format";
import type { RunResult } from "./types";

function reach(result: RunResult | undefined): string {
  const kpis = result?.kpis;
  return kpis ? ` · ${kpis.reached} of ${kpis.total} returns touched` : "";
}

/** A run's outcome in a few words. The history adds the reach; the steps strip leaves it to the panels below it. */
export function summaryOf(status: string, result: RunResult | undefined, withReach = true): string {
  const errors = result?.errors?.length ?? 0;
  const reached = withReach ? reach(result) : "";
  switch (status) {
    case "running": return "Running";
    case "waiting_for_approval": return `Awaiting approval${reached}`;
    case "what_if": return `What-if, nothing applied${reached}`;
    case "applied": return `Applied${reached}`;
    case "turned_down": return "Turned down";
    case "rejected": return errors ? `Failed validation · ${plural(errors, "error")}` : "Failed validation";
    case "stopped": return "Stopped: a return outside the reach changed";
    case "answered": return result?.client ? `Answered about ${result.client}` : "Answered";
    case "unsupported": return "Not run: question not recognized";
    case "stale": return "Approval refused: preview changed";
    case "history_full": return "Not applied: the session is full";
    case "incomplete": return "Connection closed before the run finished";
    case "cancelled": return "Cancelled";
    default: return "Stopped on an error";
  }
}

export function announcement(status: string, result: RunResult | undefined): string {
  const kpis = result?.kpis;
  const reached = kpis ? ` ${kpis.reached} of ${kpis.total} returns touched.` : "";
  switch (status) {
    case "waiting_for_approval": return `Awaiting approval.${reached} Nothing changes until you approve.`;
    case "what_if": return `What-if: nothing was applied.${reached} The rule pack and the book are unchanged.`;
    case "applied": return `Applied.${reached} The notes, the letters and the answer are written.`;
    case "turned_down": return "Turned down. Nothing was applied.";
    case "rejected": return "Failed validation. Nothing was applied; the answer lists the reasons.";
    case "stopped": return "Stopped: a return outside the reach changed. Nothing was applied; the answer lists the reasons.";
    case "answered": return "Answered.";
    case "unsupported": return "Not run: question not recognized. The answer lists what this prototype can do.";
    case "history_full": return result?.message ? capFirst(result.message) : "Not applied: the session is full.";
    default: return `${summaryOf(status, result)}.`;
  }
}
