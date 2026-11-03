"use client";

import { useCallback, useEffect, useRef } from "react";

/**
 * Applies a run's events in their real order, holding each change on screen for at least `dwellMs`.
 *
 * Engine A finishes in a few milliseconds, so without a hold every node would turn done in one frame.
 * The hold only delays when a change is drawn; durations shown are the measured ones, and a step that
 * really took longer (a model call, the eCFR request) is drawn when it really finishes. A replay spaces
 * events by their recorded server times instead. With reduced motion the hold is zero.
 */
export function usePacer(dwellMs: number) {
  const timers = useRef<number[]>([]);
  const nextAt = useRef(0);

  const reset = useCallback(() => {
    timers.current.forEach((id) => window.clearTimeout(id));
    timers.current = [];
    nextAt.current = 0;
  }, []);

  /** Run fn after the previous change has been on screen for the hold, or after gapMs on a replay. */
  const schedule = useCallback(
    (fn: () => void, gapMs = 0) => {
      const now = performance.now();
      const at = Math.max(now, nextAt.current + Math.max(dwellMs, gapMs));
      nextAt.current = at;
      const delay = at - now;
      if (delay <= 0) {
        fn();
        return;
      }
      timers.current.push(window.setTimeout(fn, delay));
    },
    [dwellMs],
  );

  useEffect(() => reset, [reset]);
  return { schedule, reset };
}
