"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { ARRIVE_MS, FOLD_MS, WAIT_MS } from "@/lib/motion";

export type ArrivalPhase = "wait" | "play";

/** One element's part in a result's arrival. When a result arrives live (`result` set) the element waits, drawn as
 * before the result; it plays if it comes on screen (its top above 88% of the window) within WAIT_MS, and after
 * ARRIVE_MS it is drawn still. Not seen by then, it is drawn still at once. Undefined when nothing is to play: a
 * reload, a replay, an older result, or a part that was not seen in time. */
export function useArrival(ref: React.RefObject<Element | null>, result: object | null | undefined): ArrivalPhase | undefined {
  const key = result ?? null;
  const [state, setState] = useState<{ key: object | null; phase: ArrivalPhase | undefined }>({ key: null, phase: undefined });
  if (state.key !== key) setState({ key, phase: key ? "wait" : undefined });
  const phase = state.key === key ? state.phase : key ? "wait" : undefined;
  useEffect(() => {
    if (phase !== "wait" || !key) return;
    const settle = (next: ArrivalPhase | undefined) => setState((s) => (s.key === key && s.phase === "wait" ? { key, phase: next } : s));
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") {
      settle("play");
      return;
    }
    const still = window.setTimeout(() => settle(undefined), WAIT_MS);
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      settle("play");
    }, { rootMargin: "0px 0px -12% 0px" });
    observer.observe(node);
    return () => {
      observer.disconnect();
      window.clearTimeout(still);
    };
  }, [phase, key, ref]);
  useEffect(() => {
    if (phase !== "play" || !key) return;
    const timer = window.setTimeout(() => setState((s) => (s.key === key ? { key, phase: undefined } : s)), ARRIVE_MS);
    return () => window.clearTimeout(timer);
  }, [phase, key]);
  return phase;
}

/** True after `value` has changed, while the component is on screen, into a state `into` accepts. A step's
 * finishing move plays for that change only: never for the state a component mounts with, so a reload, a replayed
 * result or the steps opened after a run show the final state. A change made while `enabled` is false (the session
 * a reload restores) does not count either. */
export function useEntered<T>(value: T, into: (value: T) => boolean, enabled = true): boolean {
  const [seen, setSeen] = useState(value);
  const [entered, setEntered] = useState(false);
  if (!Object.is(value, seen)) {
    setSeen(value);
    setEntered(enabled && into(value));
  }
  return entered && into(value);
}

type Phase = "closed" | "opening" | "open" | "closing";

/** A region that opens to its content's height and folds back to a 10 px edge: the steps of a run, folding into
 * the one-line strip when the run ends. Height and opacity move together. Without `animate` (reduced motion, or
 * the state a reload restores) it changes at once. Folded, its content stays in the page but hidden; `live` is
 * true only while it is open or moving, so heavy parts (the graph) are mounted only then. */
export function Fold({ id, open, animate, children }: { id: string; open: boolean; animate: boolean; children: (live: boolean) => React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(open);
  const [phase, setPhase] = useState<Phase>(open ? "open" : "closed");
  if (open !== seen) {
    setSeen(open);
    setPhase(open ? (animate ? "opening" : "open") : animate ? "closing" : "closed");
  }
  // Opening: the content is drawn folded once, then the region grows, so the browser has a height to move from.
  useLayoutEffect(() => {
    if (phase !== "opening") return;
    void ref.current?.offsetHeight;
    setPhase("open");
  }, [phase]);
  useEffect(() => {
    if (phase !== "closing") return;
    const timer = window.setTimeout(() => setPhase("closed"), FOLD_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);
  const live = phase !== "closed";
  return (
    <div id={id} ref={ref} className="fold" data-shown={phase === "open"}>
      <div className="fold-inner">
        <div hidden={!live}>{children(live)}</div>
      </div>
    </div>
  );
}
