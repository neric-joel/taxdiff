// Client and change set ids ("C-017", "CS-001") never break at their hyphen: a line ending in "CS-" with "001"
// on the next reads as two things.

import { minus } from "@/lib/format";

const ID = /\b(?:CS|C)-\d{3}\b/g;

export function Ids({ text: raw }: { text: string }) {
  const text = minus(raw);
  const parts: React.ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(ID)) {
    const at = match.index ?? 0;
    if (at > last) parts.push(text.slice(last, at));
    parts.push(<span key={at} className="id">{match[0]}</span>);
    last = at + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}
