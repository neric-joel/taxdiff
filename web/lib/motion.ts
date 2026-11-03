// The timings the code needs for the motion that shows the work: when the steps have folded, how long a result's
// arrival lasts, and the order its rows and squares arrive in. The curves and durations of each move are in
// globals.css, inside prefers-reduced-motion: no-preference, so with reduced motion every state is drawn at once.

import type { CSSProperties } from "react";

import type { ImpactRow } from "./types";
import { groupByRoute } from "./worklist";

/** The steps folding into one line when a run ends, or opening again (the .fold transition). */
export const FOLD_MS = 320;
/** One part's arrival once it is on screen, from the first row to the last after value; then it is drawn still. */
export const ARRIVE_MS = 1500;
/** How long a part waits to be seen after its result arrives (time for the page to scroll to it). A part not seen
 * by then, below the fold or in a closed tab, is drawn still, so nothing stays hidden. */
export const WAIT_MS = 700;

/** A result that arrived live on this screen, and when each part of its impact list enters. */
export interface Arrival { result: object; delays: Map<string, number> }

const FIRST_MS = 80;
const STEP_MS = 40;
const LAST_MS = 560;

/** When each part of the impact list enters, in its own order: route group by route group, the rows of each in
 * turn, 40 ms apart, none later than 560 ms. Keys are "group:<route>" for a group's header row and the client id
 * for a return; the book map lights each return's square with its row, and the folded No action returns with
 * their group. */
export function arrivalDelays(rows: ImpactRow[]): Map<string, number> {
  const out = new Map<string, number>();
  let k = 0;
  const next = () => Math.min(FIRST_MS + STEP_MS * k++, LAST_MS);
  for (const group of groupByRoute(rows)) {
    const at = next();
    out.set(`group:${group.route}`, at);
    for (const row of group.rows) out.set(row.client, group.route === "no_change" ? at : next());
  }
  return out;
}

/** An inline style that hands a delay to an element and its children (CSS reads it as var(--at)). */
export function at(ms: number | undefined): CSSProperties | undefined {
  return ms === undefined ? undefined : ({ "--at": `${ms}ms` } as CSSProperties);
}
