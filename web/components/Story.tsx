"use client";

import { useRef } from "react";

import type { Story } from "@/lib/types";
import { Ids } from "./Ids";
import { useArrival, useEntered } from "./Motion";

// The guide -------------------------------------------------------------------------------------------------

// Each step's name, and the short one a phone shows.
const STEPS: [string, string][] = [["What changes", "Change"], ["Who it affects", "Who"], ["Your decision", "Decision"]];
type StepState = "todo" | "current" | "done" | "none";

function GuideStep({ n, label, short, state, still }: { n: number; label: string; short: string; state: StepState; still: boolean }) {
  const entered = useEntered(state, (s) => s === "done", !still);
  return (
    <li data-state={state} data-enter={entered || undefined} aria-current={state === "current" ? "step" : undefined}>
      {n > 1 ? <span className="guide-line" aria-hidden="true"><span /></span> : null}
      <span className="guide-n" aria-hidden="true">
        {state === "done" ? (
          <svg viewBox="0 0 12 12" width="10" height="10" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path pathLength={1} d="M2.4 6.3l2.3 2.3 4.9-5.2" />
          </svg>
        ) : n}
      </span>
      <span className="guide-label" data-text={label}><span className="on-wide">{label}</span><span className="on-phone">{short}</span></span>
      {state === "done" ? <span className="sr-only">, done</span> : state === "none" ? <span className="sr-only">, not needed for this result</span> : null}
    </li>
  );
}

/** Where the run is, in three steps: the change read and recomputed, the returns it reaches, the reviewer's
 * decision. `step` is the current one (0 before any run); `complete` marks all three done once a change is applied
 * or turned down; a result that takes no decision sets `decision` false and the third step stands aside. One quiet
 * line, not focusable: it only says where the page is. While `still` (a reload drawing the restored run) it changes
 * without moving. */
export function Guide({ step, complete, decision, still }: { step: number; complete: boolean; decision: boolean; still: boolean }) {
  return (
    <ol className="guide" id="guide" data-step={step} data-still={still || undefined} aria-label="Where this run is">
      {STEPS.map(([label, short], index) => {
        const n = index + 1;
        const state: StepState = complete || n < step ? "done" : n === step ? "current" : n === 3 && step > 0 && !decision ? "none" : "todo";
        return <GuideStep key={label} n={n} label={label} short={short} state={state} still={still} />;
      })}
    </ol>
  );
}

// One return, start to finish ---------------------------------------------------------------------------------

const BEFORE_AFTER = /([+-]?\$\d[\d,]*(?:\.\d+)?) to ([+-]?\$\d[\d,]*(?:\.\d+)?)/g;

/** The story's own words. Its client opens the return; in "from $18,000 to $6,000" the after value is set apart so
 * it can arrive after the before value. Nothing is computed here. */
function storyParts(story: Story, onClient: (client: string) => void): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const words = (text: string) => {
    let last = 0;
    for (const match of text.matchAll(BEFORE_AFTER)) {
      const start = match.index ?? 0;
      if (start > last) out.push(<Ids key={out.length} text={text.slice(last, start)} />);
      out.push(<span key={out.length} className="nowrap"><span className="was">{match[1]}</span> to <span className="now">{match[2]}</span></span>);
      last = start + match[0].length;
    }
    if (last < text.length) out.push(<Ids key={out.length} text={text.slice(last)} />);
  };
  const at = story.client ? story.text.indexOf(story.client) : -1;
  if (at < 0) {
    words(story.text);
    return out;
  }
  words(story.text.slice(0, at));
  out.push(<button key={out.length} type="button" className="fig id" onClick={() => onClient(story.client)} title={`Show ${story.client}`}>{story.client}</button>);
  words(story.text.slice(at + story.client.length));
  return out;
}

/** The first thing in a result: one reached return from the change to its next step. When its result arrives live
 * (arrive), the card rises in and the after value follows the before value. */
export function StoryCard({ story, arrive, onClient }: { story: Story; arrive: object | null; onClient: (client: string) => void }) {
  const ref = useRef<HTMLElement>(null);
  const phase = useArrival(ref, arrive);
  return (
    <section className="panel story" id="story" ref={ref} aria-labelledby="story-title" data-arrive={phase}>
      <div className="panel-head">
        <h2 id="story-title" tabIndex={-1}>One return, start to finish</h2>
        {story.change ? <span className="end run-status id">{story.change}</span> : null}
      </div>
      <p className="story-text">{storyParts(story, onClient)}</p>
    </section>
  );
}
