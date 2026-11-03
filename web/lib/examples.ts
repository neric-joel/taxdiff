// The example chips in plain words: what each one does, its change set id last, and four short groups. The
// API's own label is the fallback for an example this list does not know.

import type { Example } from "./types";

export interface ChipView { label: string; code: string | null; group: string; needs: string | null }

export const GROUPS = ["Start here", "After the overtime rule", "Other changes", "Checks that refuse bad input"];

const PLAIN: Record<string, { label: string; code?: string; group: string }> = {
  fix: { label: "Apply the IRS overtime rule", code: "CS-001", group: "Start here" },
  evidence: { label: "Where C-017's overtime deduction comes from", group: "Start here" },
  mfs: { label: "Why C-010's overtime deduction is zero", group: "Start here" },
  document: { label: "Read C-017's corrected employer statement", group: "After the overtime rule" },
  "why-up": { label: "Did C-017's tax go down?", group: "After the overtime rule" },
  "why-down": { label: "Why did C-031's tax go down?", group: "After the overtime rule" },
  amend: { label: "Which filed returns need amending?", group: "After the overtime rule" },
  w2c: { label: "Apply C-021's corrected W-2 (wages $149,000)", group: "Other changes" },
  "what-if": { label: "What if the overtime cap drops to $10,000?", group: "Other changes" },
  "invalid-900": { label: "Refuse a negative cap with no citation", code: "CS-900", group: "Checks that refuse bad input" },
  "invalid-901": { label: "Refuse a statement for the wrong year", code: "CS-901", group: "Checks that refuse bad input" },
};

/** The change set an example only makes sense after: the API's needs, else the "After CS-001: " of its label. */
export function needsOf(example: Example): string | null {
  return example.needs || /^After (CS-\d+):/.exec(example.label)?.[1] || null;
}

export function chipView(example: Example): ChipView {
  const known = PLAIN[example.id];
  if (known) return { label: known.label, code: known.code ?? null, group: known.group, needs: needsOf(example) };
  const bare = example.label.replace(/^After CS-\d+: /, "");
  const code = /^(CS-\d+):\s*/.exec(bare);
  return {
    label: code ? bare.slice(code[0].length).replace(/^./, (c) => c.toUpperCase()) : bare,
    code: code ? code[1] : null,
    group: needsOf(example) ? "After the overtime rule" : "Other changes",
    needs: needsOf(example),
  };
}

/** The examples in their groups, in the groups' order. */
export function grouped(examples: Example[]): { group: string; items: { example: Example; view: ChipView }[] }[] {
  const views = examples.map((example) => ({ example, view: chipView(example) }));
  return GROUPS.map((group) => ({ group, items: views.filter((item) => item.view.group === group) })).filter((entry) => entry.items.length > 0);
}

/** The words a chip is read as: its label, its code, and whether it needs a change first. */
export function chipName(view: ChipView, met: boolean): string {
  return `${view.label}${view.code ? ` (${view.code})` : ""}${view.needs && !met ? `, needs ${view.needs} first` : ""}`;
}
