import type { Amount, RouteKey, ServiceStatus } from "./types";

// The same labels as ROUTES in src/taxdiff/router.py.
export const ROUTE_LABELS: Record<RouteKey, string> = {
  update: "Update return",
  reviewer: "Back to reviewer",
  amendment: "Amendment review",
  document: "Document request",
  below_materiality: "Too small to amend: tell the client",
  no_change: "No action",
};
// Most urgent first. The impact list, the route bar and the approval summary all use this order.
export const ROUTE_ORDER: RouteKey[] = ["amendment", "document", "reviewer", "update", "below_materiality", "no_change"];

export const STATUS_LABELS: Record<string, string> = { prep: "In preparation", review: "In review", filed: "Filed" };
export const SHORT_STATUS: Record<string, string> = { prep: "Prep", review: "Review", filed: "Filed" };
export const FILING_LABELS: Record<string, string> = {
  single: "Single", mfj: "Married filing jointly", mfs: "Married filing separately", hoh: "Head of household",
};
export const SHORT_FILING: Record<string, string> = { single: "Single", mfj: "Joint", mfs: "Separate", hoh: "Head of household" };
export const PARITY_LABELS: Record<string, string> = { agree: "Agrees", differs: "Differs", not_checked: "Not checked" };

/** An amount as the screen shows it: a true minus sign ("−$660") where the API wrote a hyphen. Display only; the
 * CSV export keeps the API's plain numbers. */
export function minus(text: string): string {
  return text.replace(/(^|[^\w])-(?=\$\d)/g, "$1\u2212");
}

/** The text the engine printed for an amount, or a grey dash for a value that does not apply. */
export function amountText(amount: Amount | null | undefined): string {
  if (!amount || amount.value === null && !amount.unknown) return "";
  return minus(amount.text);
}

/** A tax change as the API wrote it: the amount, the range it is bounded to while a document is outstanding,
 * or "unknown". */
export function changeText(amount: Amount | null | undefined): string {
  if (!amount) return "";
  return amount.value !== null || amount.range ? minus(amount.text) : "unknown";
}

export function timeOf(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "10:23", on a 24-hour clock, in the viewer's time zone. */
export function clockOf(iso: string | null | undefined): string {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "";
}

/** "10:23:07", the same clock with seconds. */
export function secondsOf(iso: string | null | undefined): string {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }) : "";
}

export function dateOf(iso: string | null | undefined): string {
  const date = iso ? new Date(iso) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" }) : "";
}

export const SOURCE_LABELS: Record<string, string> = { live: "Live", cached: "Cached", mock: "Mock", fallback: "Fallback" };

/** "1 return", "2 returns". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function capFirst(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** "Qualified overtime" reads "qualified overtime" inside a sentence; "AGI" stays as it is. */
export function lowerFirst(text: string): string {
  return text.length > 1 && /[a-z]/.test(text[1]) ? text[0].toLowerCase() + text.slice(1) : text;
}

/** Where a line sits on the forms: "1-A 21" is Schedule 1-A line 21, "16" is Form 1040 line 16. */
export function lineRef(no: string): string {
  if (!no) return "";
  return no.startsWith("1-A ") ? `Schedule 1-A, line ${no.slice(4)}` : `Form 1040, line ${no}`;
}

/** A line label's short name: "Qualified overtime, Schedule 1-A part III" reads "Qualified overtime". */
export function lineName(label: string): string {
  return label.split(",")[0];
}

export const MATERIALITY_HELP =
  "A filed return whose new tax differs from the filed return by less than this amount is below materiality: the client is told, and the return does not go to amendment review.";

/** Status and filing status in full on a wide screen, short on a phone: "In review · Married filing jointly". */
export function filingWords(status: string, filing: string): { long: string; short: string } {
  return {
    long: `${STATUS_LABELS[status] ?? status} · ${FILING_LABELS[filing] ?? filing}`,
    short: `${SHORT_STATUS[status] ?? status} · ${SHORT_FILING[filing] ?? filing}`,
  };
}

/** How the cross-check runs on this server, in words: the API's label ("PolicyEngine-US, live API",
 * "PolicyEngine-US 2.90.4, cached results"), or the same words built from its state and release. */
/** The cross-check's name as one run used it: the live API, an answer the server reused, or results saved
 * earlier. Without the run's step it falls back to the service's own state. */
export function crossCheckLabel(status: ServiceStatus | null | undefined, node: { source?: string | null; provider?: string } | undefined): string {
  if (!node?.source) return policyEngineLabel(status);
  const api = /PolicyEngine API/.test(node.provider ?? "");
  const release = (node.provider ?? "").match(/PolicyEngine-US (\S+)/)?.[1];
  const named = release ? `PolicyEngine-US ${release}` : "PolicyEngine-US";
  if (node.source === "live") return api ? "PolicyEngine-US, live API (release not reported)" : `${named}, live`;
  if (node.source === "cached") return api ? "PolicyEngine-US: cached answer from the live API" : `${named}, cached results`;
  return `${named}, saved results`;
}

export function policyEngineLabel(status: ServiceStatus | null | undefined): string {
  if (!status) return "PolicyEngine-US";
  if (status.label) return status.label;
  const release = status.release && !/policyengine\.org/i.test(status.release) ? ` ${status.release}` : "";
  if (status.state === "live") return release ? `PolicyEngine-US${release}, live` : "PolicyEngine-US, live API";
  if (status.state === "cached") return `PolicyEngine-US${release}, cached results`;
  return `PolicyEngine-US${release}`;
}
