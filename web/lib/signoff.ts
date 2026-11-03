// Per-return sign-off after a change is applied: who signs which route, the choices, and the progress count.
// A sign-off is a record the tab keeps with the session; the name is typed, not verified.

import type { ImpactRow, RouteKey } from "./types";

export type Role = "Preparer" | "Reviewer" | "Partner";
export type SignDecision = "accept" | "hold" | "reassign" | "client_amend" | "client_declined";

export interface SignOff {
  at: string; // local time, "2026-11-02T10:42:07"
  change_id: string;
  client: string;
  route: RouteKey;
  decision: SignDecision;
  name: string;
  role: Role;
}

/** Who signs a return off on each route. No action needs no sign-off. */
export function roleFor(route: RouteKey): Role | null {
  if (route === "update") return "Preparer";
  if (route === "amendment") return "Partner";
  if (route === "reviewer" || route === "document" || route === "below_materiality") return "Reviewer";
  return null;
}

/** The words above a row's sign-off buttons. */
export function controlLabel(route: RouteKey): string {
  if (route === "below_materiality") return "Client's choice, recorded by the reviewer";
  const role = roleFor(route);
  return role ? `${role} sign-off` : "";
}

export interface Choice { decision: SignDecision; button: string; label: string }

/** The buttons for a route. button is what the button shows; label is the decision as recorded. */
export function choicesFor(route: RouteKey): Choice[] {
  if (route === "below_materiality") {
    return [
      { decision: "client_amend", button: "Asked for an amended return", label: "Client asked for an amended return" },
      { decision: "client_declined", button: "Declined", label: "Client declined" },
    ];
  }
  return [
    { decision: "accept", button: "Accept", label: "Accept" },
    { decision: "hold", button: "Hold", label: "Hold" },
    { decision: "reassign", button: "Reassign", label: "Reassign" },
  ];
}

/** The decision in a sentence's words: "Accepted", "Client declined". */
export const DECISION_WORDS: Record<SignDecision, string> = {
  accept: "Accepted",
  hold: "Held",
  reassign: "Reassigned",
  client_amend: "Client asked for an amended return",
  client_declined: "Client declined",
};

/** The returns of an applied change that take a sign-off: every reached return except No action. */
export function signable(rows: ImpactRow[]): ImpactRow[] {
  return rows.filter((row) => roleFor(row.route) !== null);
}

/** The latest sign-off of each return for one change. */
export function latestFor(signoffs: SignOff[], changeId: string): Map<string, SignOff> {
  const out = new Map<string, SignOff>();
  for (const item of signoffs) if (item.change_id === changeId) out.set(item.client, item);
  return out;
}

/** "Signed off 6 of 14": the signable returns with a recorded decision. */
export function progress(rows: ImpactRow[], latest: Map<string, SignOff>): { done: number; total: number } {
  const list = signable(rows);
  return { done: list.filter((row) => latest.has(row.client)).length, total: list.length };
}
