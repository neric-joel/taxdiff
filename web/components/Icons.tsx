import type { IconName } from "@/lib/graph";
import type { NodeState } from "@/lib/types";

const PATHS: Record<IconName, string> = {
  message: "M2.5 3.5h11v7.5h-6l-3 2.5V11h-2z",
  reader: "M3 4h10M3 7h10M3 10h6M11.5 9.5v4M10 13.5h3",
  file: "M4 2.5h5l3 3v8H4zM9 2.5v3h3",
  check: "M8 2l5 2v4c0 3-2.2 5-5 6-2.8-1-5-3-5-6V4zM5.7 8.1l1.6 1.6 3-3.3",
  calc: "M3.5 2.5h9v11h-9zM5.5 4.5h5v2.2h-5zM5.6 9.2h.1M8 9.2h.1M10.4 9.2h.1M5.6 11.5h.1M8 11.5h.1M10.4 11.5h.1",
  scale: "M8 3v10.5M4.5 13.5h7M3 5h10M8 3.2v1.8M3 5l-1.6 4.2h3.2zM13 5l-1.6 4.2h3.2z",
  book: "M2.5 3.5H7a1.5 1.5 0 0 1 1 .4V13a1.7 1.7 0 0 0-1-.4H2.5zM13.5 3.5H9a1.5 1.5 0 0 0-1 .4V13a1.7 1.7 0 0 1 1-.4h4.5z",
  split: "M2 8h4l3.2-4.5H13M6 8l3.2 4.5H13M11.3 1.8l1.9 1.7-1.9 1.7M11.3 10.8l1.9 1.7-1.9 1.7",
  hand: "M3.4 12.6l.9-3.4 6.3-6.3 2.5 2.5-6.3 6.3zM9.5 4l2.5 2.5M2.5 14.5h11",
  note: "M3.5 2.5h9v11h-9zM5.5 5.5h5M5.5 8h5M5.5 10.5h3",
  answer: "M2.5 3.5h11v7.5h-6l-3 2.5V11h-2zM5 6.2h6M5 8.4h3.6",
  list: "M5.8 4h7.7M5.8 8h7.7M5.8 12h7.7M2.6 4h.2M2.6 8h.2M2.6 12h.2",
};

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.4}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}

/** The state mark at the left of a node: its shape, not only its colour, says the state. A finished step is a
 * filled disc in its state's colour with the mark cut out of it; each stroke of the mark has a length of 1, so it
 * can be drawn in when the step finishes on screen. */
export function StateGlyph({ state }: { state: NodeState }) {
  const ring = <circle cx="9" cy="9" r="7" />;
  const disc = <circle className="disc" cx="9" cy="9" r="7.75" />;
  return (
    <svg className="glyph" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth={1.5}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {state === "idle" && <circle cx="9" cy="9" r="2.5" />}
      {state === "queued" && <circle cx="9" cy="9" r="7" strokeDasharray="2.5 2.5" />}
      {state === "running" && (
        <g className="spin">
          <circle cx="9" cy="9" r="7" opacity={0.25} />
          <path d="M9 2a7 7 0 0 1 7 7" />
        </g>
      )}
      {state === "done" && (<>{disc}<path className="mark" pathLength={1} d="M5.5 9.3l2.4 2.4 4.6-5" /></>)}
      {state === "failed" && (<>{disc}<path className="mark" pathLength={1} d="M6.3 6.3l5.4 5.4" /><path className="mark" pathLength={1} d="M11.7 6.3l-5.4 5.4" /></>)}
      {state === "skipped" && (<><circle cx="9" cy="9" r="7" strokeDasharray="2.5 2.5" /><path d="M5.8 9h6.4" /></>)}
      {state === "waiting" && (<>{ring}<path d="M7.3 6v6M10.7 6v6" /></>)}
    </svg>
  );
}

export function Mark() {
  // A form's part label holding three arcs: the reach of one change across the book.
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <rect width="22" height="22" rx="2" fill="var(--label-bg)" />
      <g fill="none" stroke="var(--label-ink)" strokeWidth="1.6">
        <path d="M5 17a3 3 0 0 1 3-3" />
        <path d="M5 17a6.5 6.5 0 0 1 6.5-6.5" />
        <path d="M5 17a10 10 0 0 1 10-10" />
      </g>
      <circle cx="5" cy="17" r="1.6" fill="var(--label-ink)" />
    </svg>
  );
}
