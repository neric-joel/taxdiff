"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { ApiError, getBook, getExamples, getHealth, streamRun, wasBuffered, type DecisionBody } from "@/lib/api";
import { dollars, localIso, materialityEvent, resetEvent, signOffEvent, undoEvent, withUtc } from "@/lib/audit";
import { saveFile } from "@/lib/files";
import { capFirst, clockOf, crossCheckLabel, dateOf, lineName, lowerFirst, plural, policyEngineLabel } from "@/lib/format";
import { NODES, applyEvent, formatMs, idleView, stepCounts, type GraphView } from "@/lib/graph";
import { FOLD_MS, arrivalDelays, type Arrival } from "@/lib/motion";
import { usePacer } from "@/lib/pacer";
import { DECISION_WORDS, latestFor, roleFor, type SignDecision, type SignOff } from "@/lib/signoff";
import { emptySession, loadSession, readFlag, saveSession, writeFlag, type SessionState } from "@/lib/store";
import { announcement, summaryOf } from "@/lib/summary";
import type { AuditEvent, Book, Example, Health, ImpactRow, Link, NodeEvent, NodeId, RunRecord, RunResult, StreamEvent } from "@/lib/types";
import { AuditPanel } from "./Audit";
import { ApprovalGate, DecisionLine, approvalKind } from "./Gate";
import { Stepper, WorkflowGraph } from "./Graph";
import { Mark } from "./Icons";
import { Fold } from "./Motion";
import { NotesPanel, type Wording } from "./Notes";
import { AboutDrawer, ConfirmDialog, NodeDrawer, ReturnDrawer, type ConfirmSpec } from "./Overlays";
import { AnswerCard, ImpactPanel, ReachStrip, type SignControls } from "./Panels";
import { ResultTabs, type ResultTab } from "./ResultTabs";
import { SidePanel, type ApprovedItem, type Notice, type SideTab } from "./Side";
import { Guide, StoryCard } from "./Story";

type DrawerState = { kind: "node"; id: NodeId } | { kind: "return"; client: string; line?: string } | { kind: "about"; settings?: boolean } | null;
/** The run status the screen shows. It changes through the pacer, together with the panels, so the header
 * never says a run has ended before its result is drawn. */
interface Paced { status: string; totalMs: number }

const DWELL_MS = 300;
const BOOTSTRAP_TIMEOUT_MS = 15_000;
const RUN_TIMEOUT_MS = 100_000; // the server stops a run after 90 s; the browser waits a little longer
const ENDED = ["incomplete", "error", "stale"];
// Results that show a book the session does not hold: a preview waiting at Approve, and a what-if, which
// is a preview only. Neither is stored as the current book, and neither changes the rule pack or the facts.
const PREVIEWS = ["waiting", "what_if"];
const QUESTION_CHARS = 500;
const HISTORY_LIMIT = 25; // approved changes the server replays; /api/health says the same in limits.history
const MAX_USED_IDS = 899;

function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

function useTheme() {
  const [choice, setChoice] = useState<"system" | "light" | "dark">("system");
  const systemDark = useMediaQuery("(prefers-color-scheme: dark)");
  useEffect(() => {
    const saved = readFlag("theme");
    if (saved === "light" || saved === "dark") setChoice(saved);
  }, []);
  useEffect(() => {
    const root = document.documentElement;
    if (choice === "system") delete root.dataset.theme;
    else root.dataset.theme = choice;
    writeFlag("theme", choice);
  }, [choice]);
  const resolved: "light" | "dark" = choice === "system" ? (systemDark ? "dark" : "light") : choice;
  return { choice, setChoice, resolved };
}

/** The graph as a run's events leave it. A step still open when the run ended is shown as failed. */
function viewFrom(record: RunRecord): GraphView {
  const view = record.events.reduce((v, event) => (event.type === "node" ? applyEvent(v, event) : v), idleView());
  if (ENDED.includes(record.status)) {
    for (const node of NODES) {
      if (view[node.id].state === "running" || view[node.id].state === "queued") {
        view[node.id] = { ...view[node.id], state: "failed", summary: "the run ended before this step finished" };
      }
    }
  }
  return view;
}

/** The last finished event of one step in a run (the one its drawer shows). */
function lastNode(record: RunRecord | undefined, id: NodeId): NodeEvent | undefined {
  const events = record?.events ?? [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.type === "node" && event.node === id && event.state !== "running" && event.state !== "queued") return event;
  }
  return undefined;
}

/** Which model service a step called and which model, from its recorded calls or its provider line
 * ("Anthropic API (claude-haiku-5-5), figures checked", "Ollama, local (qwen3.5:2b)"). */
function modelOf(event: NodeEvent | undefined, fallbackModel?: string): { ollama: boolean; model?: string } {
  const call = event?.request?.calls?.find((c) => c.model);
  const text = event?.provider ?? "";
  const named = /^(?:Anthropic API|Ollama[^(]*)\s*\(([^)]+)\)/.exec(text)?.[1];
  const ollama = call?.provider === "ollama" || /^Ollama/i.test(text);
  return { ollama, model: call?.model ?? (named && named !== "paid" ? named : ollama ? undefined : fallbackModel) };
}

interface ServiceLine { name: string; fine: boolean; text: string }

/** Each outside service as /api/health reports it, and whether a step runs a fallback because of it. A local
 * model in place of the Anthropic API, and cached PolicyEngine-US results, are how the server is set up. */
function serviceLines(s: Health["services"]): ServiceLine[] {
  const a = s.anthropic;
  const lines: ServiceLine[] = [
    a.state === "ok" ? { name: "Anthropic API", fine: true, text: "answering" }
      : a.state === "other" ? { name: "Anthropic API", fine: true, text: `not used${a.detail ? `; ${a.detail}` : ""}` }
      : { name: "Anthropic API", fine: false, text: `${a.state === "error" ? "did not answer" : /turned off/i.test(a.detail ?? "") ? "turned off" : "not configured"}; the pattern reader and fixed templates run` },
    s.ecfr.state === "ok" ? { name: "eCFR API", fine: true, text: "answering" }
      : { name: "eCFR API", fine: false, text: `${s.ecfr.state === "error" ? "did not answer" : "not called"}; the saved excerpts are read` },
  ];
  const pe = s.policyengine;
  const label = policyEngineLabel(pe).replace(/^PolicyEngine-US,?\s*/, "");
  const when = dateOf(pe.cached_at);
  lines.push(pe.state === "live" || pe.state === "cached"
    ? { name: "PolicyEngine-US", fine: true, text: `${label || pe.state}${pe.state === "cached" && when ? `, computed ${when}` : ""}` }
    : { name: "PolicyEngine-US", fine: false, text: pe.detail || pe.state });
  for (const [key, name] of [["federal_register", "Federal Register API"], ["irs", "irs.gov"]] as const) {
    const status = s[key];
    if (status) lines.push(status.state === "ok" || status.state === "live" ? { name, fine: true, text: "answering" } : { name, fine: false, text: status.detail || status.state });
  }
  return lines;
}

function servicesSummary(health: Health | null, down: boolean): { state: string; text: string; title: string } {
  if (!health) return down ? { state: "down", text: "not answering", title: "The server did not answer its health check." } : { state: "checking", text: "checking", title: "Checking the outside services" };
  const lines = serviceLines(health.services);
  const fallback = lines.filter((line) => !line.fine).length;
  return {
    state: fallback ? "fallback" : "ok",
    text: fallback ? `${fallback} on a fallback` : "all answering",
    title: lines.map((line) => `${line.name}: ${line.text}`).join("\n"),
  };
}

/** The line that replaces the approval once the reviewer decides: "CS-001 applied by A. Reviewer at 10:23 ·
 * technical approval of the rule change · rules version 1 to 2". Empty for any other result. */
function decisionOf(record: RunRecord | undefined, result: RunResult | undefined): string {
  if (!record || !result || (result.status !== "applied" && result.status !== "turned_down")) return "";
  const started = [...record.events].reverse().find((event) => event.type === "run_started" && event.mode === "decision");
  if (!started || started.type !== "run_started") return "";
  const output = lastNode(record, "approve")?.output as { approver?: string } | null | undefined;
  const name = output?.approver || result.history_item?.approver || result.audit?.find((event) => event.approver)?.approver || "the reviewer";
  const id = result.change_view?.id ?? result.change?.id ?? "The change";
  const at = clockOf(started.at);
  const from = result.pack_before?.version;
  const to = result.pack_after?.version;
  const client = result.change_view?.client;
  const parts = [`${id} ${result.status === "applied" ? "applied" : "turned down"} by ${name}${at ? ` at ${at}` : ""}`,
    lowerFirst(approvalKind(result.change_view?.kind ?? (result.change?.kind as string | undefined)))];
  if (result.status === "turned_down") parts.push(from !== undefined ? `nothing applied, rules stay at version ${from}` : "nothing applied");
  else if (from !== undefined) parts.push(to !== undefined && to !== from ? `rules version ${from} to ${to}` : `rules stay at version ${from}`);
  if (result.status === "applied" && client && result.facts_after?.[client]) parts.push(`${client} facts now v${result.facts_after[client]}`);
  return parts.join(" · ");
}

/** How many change ids this session has shown, so the server gives a new preview a fresh one. */
function usedIds(audit: AuditEvent[]): number {
  return Math.min(MAX_USED_IDS, new Set(audit.map((event) => event.change_id).filter((id) => id && id !== "-")).size);
}

function withTimeout<T>(start: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), BOOTSTRAP_TIMEOUT_MS);
  return start(controller.signal).finally(() => window.clearTimeout(timer));
}

export default function Dashboard() {
  const [session, setSession] = useState<SessionState>(emptySession);
  const [loaded, setLoaded] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [apiDown, setApiDown] = useState<string | null>(null);
  const [examples, setExamples] = useState<Example[] | null>(null);
  const [book, setBook] = useState<Book | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [view, setView] = useState<GraphView>(idleView);
  const [shown, setShown] = useState<RunResult | undefined>(undefined);
  const [paced, setPaced] = useState<Paced | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [busy, setBusy] = useState<"question" | "decision" | null>(null);
  const [warming, setWarming] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [drawer, setDrawer] = useState<DrawerState>(null);
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [selectedClient, setSelectedClient] = useState<string | null>(null);
  const [bandOpen, setBandOpen] = useState(true);
  const [stepsOpen, setStepsOpen] = useState(false);
  const [replaying, setReplaying] = useState(false);
  const [question, setQuestion] = useState("");
  const [howOpen, setHowOpen] = useState(false);
  const [tab, setTab] = useState<SideTab>("ask");
  const [resultTab, setResultTab] = useState<ResultTab>("impact");
  const [status, setStatus] = useState("");
  // The result that arrived live on this screen: its story, squares, rows and decision line each enter once, when
  // they are on screen. A replay or a reload shows the final state.
  const [arrival, setArrival] = useState<RunResult | null>(null);
  // Motion starts after the restored session is drawn, so a reload never animates.
  const [motionReady, setMotionReady] = useState(false);
  const theme = useTheme();
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const phone = useMediaQuery("(max-width: 767px)");
  const pacer = usePacer(reduced ? 0 : DWELL_MS);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const replayTimer = useRef<number | null>(null);
  // Where focus goes when the run on screen ends: the result's heading, or the line that replaces the approval.
  const focusNext = useRef<"result" | "decision" | null>(null);
  // A question run brings its steps into view when they start below the screen (a phone, a narrow window).
  const showRun = useRef(false);

  /** Start drawing something new on the graph: cancels every queued change of the previous drawing. */
  const fresh = useCallback(() => {
    pacer.reset();
    if (replayTimer.current !== null) window.clearTimeout(replayTimer.current);
    replayTimer.current = null;
  }, [pacer]);

  // Load the session and the server's state ------------------------------------------------------------
  useEffect(() => {
    const loadedSession = loadSession();
    // A run that was still streaming when the page closed never finished; say so instead of "Running".
    const stored = { ...loadedSession, runs: loadedSession.runs.map((run) => (run.status === "running" ? { ...run, status: "incomplete" } : run)) };
    setSession(stored);
    setLoaded(true);
    const last = stored.runs[stored.runs.length - 1];
    if (last) {
      setActiveId(last.id);
      setView(viewFrom(last));
      setShown(last.result);
      setPaced({ status: last.status, totalMs: last.totalMs });
    }
    setStepsOpen(readFlag("steps-open") === "1");
    setBandOpen(!last && readFlag("band-hidden") !== "1");
  }, []);

  useEffect(() => { if (loaded) setSaveFailed(!saveSession(session)); }, [session, loaded]);

  // Two frames after the restored session is drawn, so the browser has drawn it still before anything may move.
  useEffect(() => {
    if (!loaded) return;
    let second = 0;
    const first = window.requestAnimationFrame(() => { second = window.requestAnimationFrame(() => setMotionReady(true)); });
    return () => { window.cancelAnimationFrame(first); window.cancelAnimationFrame(second); };
  }, [loaded]);

  const refreshHealth = useCallback(async () => {
    const warmTimer = window.setTimeout(() => setWarming(true), 1200);
    const [h, e, b] = await Promise.allSettled([withTimeout(getHealth), withTimeout(getExamples), withTimeout(getBook)]);
    window.clearTimeout(warmTimer);
    setWarming(false);
    if (h.status === "fulfilled") setHealth(h.value);
    if (e.status === "fulfilled") setExamples(e.value.examples);
    if (b.status === "fulfilled") setBook(b.value);
    const failed = [h, e, b].filter((r) => r.status === "rejected");
    setApiDown(failed.length ? (failed.length === 3 ? "The server did not respond within 15 s." : "Part of the page did not load: the server did not respond within 15 s.") : null);
  }, []);
  useEffect(() => { void refreshHealth(); }, [refreshHealth]);

  const active = session.runs.find((run) => run.id === activeId);
  const historyIds = session.history.map((item) => item.change.id);
  const historyKey = historyIds.join(",");
  const pacedStatus = paced?.status ?? null;
  const basePack = book?.pack ?? health?.pack;

  // When the run on screen ends, its result comes first: focus moves to its heading (the approval's, the answer's,
  // or the line that replaces the approval), and the result is scrolled into view from the steps strip down. The
  // steps fold first, so the scroll is worked out on the folded layout.
  useEffect(() => {
    if (!pacedStatus || pacedStatus === "running" || !focusNext.current) return;
    const want = focusNext.current;
    focusNext.current = null;
    // On a phone the story is what the screen shows first (the approval's bar sits at the foot of the screen).
    const target = (want === "decision" ? document.getElementById("decision") : null)
      ?? (phone ? document.getElementById("story-title") : null)
      ?? document.getElementById("gate-title") ?? document.getElementById("answer-title");
    if (!target) return;
    target.focus({ preventScroll: true });
    if (phone && want === "decision") {
      // On a phone the decision line, and the worklist under it, lead once the decision is made.
      target.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
      return;
    }
    const settle = () => {
      // Scroll only when the strip is hidden or low on the screen, so it stays in view above the result.
      const anchor = document.getElementById("steps") ?? target;
      const top = anchor.getBoundingClientRect().top;
      const clear = parseFloat(getComputedStyle(anchor).scrollMarginTop) || 0;
      if (top < clear || top > window.innerHeight * 0.4) anchor.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    };
    if (reduced) {
      settle();
      return;
    }
    // A reader who scrolls while the steps fold keeps the place they scrolled to.
    const events = ["wheel", "touchmove", "keydown"] as const;
    const stop = () => { window.clearTimeout(timer); events.forEach((name) => window.removeEventListener(name, stop)); };
    const timer = window.setTimeout(() => { stop(); settle(); }, FOLD_MS);
    events.forEach((name) => window.addEventListener(name, stop, { passive: true }));
    return stop;
  }, [pacedStatus, reduced]);

  // A question run that starts below the screen (the steps sit under the question panel on a phone) brings its
  // steps into view, so the reader sees the work happen.
  useEffect(() => {
    if (pacedStatus !== "running" || !showRun.current) return;
    showRun.current = false;
    const steps = document.getElementById("steps");
    if (!steps) return;
    const top = steps.getBoundingClientRect().top;
    if (top < 0 || top > window.innerHeight * 0.6) steps.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
  }, [pacedStatus, reduced]);

  // Streaming ---------------------------------------------------------------------------------------------
  const appendEvent = useCallback((id: string, event: StreamEvent, ms: number) => {
    setSession((s) => {
      const runs = s.runs.map((run) => {
        if (run.id !== id) return run;
        const next: RunRecord = { ...run, events: [...run.events, event], receivedAt: [...run.receivedAt, ms] };
        if (event.type === "result") {
          next.result = event.result;
          if (event.result.status === "waiting") next.preview = event.result;
        }
        if (event.type === "run_finished") {
          next.status = event.status;
          next.totalMs = run.totalMs + event.total_ms;
        }
        return next;
      });
      const next = { ...s, runs };
      if (event.type === "result") {
        const result = event.result;
        // The run's latest start (a decision follows its question) gives the server's clock in UTC.
        const record = s.runs.find((run) => run.id === id);
        const starts = (record?.events ?? []).filter((e) => e.type === "run_started");
        const started = starts[starts.length - 1];
        next.audit = [...s.audit, ...withUtc(result.audit ?? [], started?.type === "run_started" ? started.at : undefined)];
        if (result.status === "applied" && result.history_item) {
          const item = result.history_item;
          // What Undo restores: the session as it stood before this change.
          next.checkpoints = [...s.checkpoints.slice(0, s.history.length), {
            change_id: item.change.id, title: item.change.title, approver: item.approver, at: localIso(),
            pack: s.pack, facts: s.facts, book: s.currentBook,
          }];
          next.history = [...s.history, item];
          next.pack = result.pack_after;
          next.facts = result.facts_after;
        }
        // The current book: any result that shows it, except a preview or a what-if, which show the book
        // the change would make.
        if (result.book && !PREVIEWS.includes(result.status)) next.currentBook = result.book;
      }
      return next;
    });
  }, []);

  const replay = useCallback((record: RunRecord, label = true) => {
    fresh();
    setActiveId(record.id);
    setView(idleView());
    setShown(undefined);
    setArrival(null);
    setResultTab("impact");
    setReplaying(label);
    setDeciding(false);
    setPaced({ status: "running", totalMs: 0 });
    focusNext.current = "result";
    let previous = 0;
    for (const event of record.events) {
      if (event.type === "run_started") { previous = event.t; continue; }
      const gap = Math.max(0, event.t - previous);
      previous = event.t;
      if (event.type === "node") pacer.schedule(() => setView((v) => applyEvent(v, event)), gap);
      else if (event.type === "result") pacer.schedule(() => setShown(event.result), gap);
    }
    pacer.schedule(() => {
      if (ENDED.includes(record.status)) setView(viewFrom(record));
      setPaced({ status: record.status, totalMs: record.totalMs });
    }, 0);
  }, [fresh, pacer]);

  const runStream = useCallback(async (path: "/api/run" | "/api/approve", body: unknown, onStarted: (id: string) => string | null) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), RUN_TIMEOUT_MS);
    const warmTimer = window.setTimeout(() => setWarming(true), 1500);
    const serverT: number[] = [];
    const receivedT: number[] = [];
    let id: string | null = null;
    let finished = false;
    let lastResult: RunResult | undefined;
    const close = (status: string) => {
      const runId = id;
      if (!runId) return;
      setSession((s) => ({ ...s, runs: s.runs.map((run) => (run.id === runId && run.status === "running" ? { ...run, status } : run)) }));
    };
    // A run that ends without its last event: a decision goes back to the waiting preview, so it can be
    // tried again; a question run ends with the status given.
    const endEarly = (status: string) => {
      const record = id ? sessionRef.current.runs.find((run) => run.id === id) : undefined;
      setPaced(record ? { status: record.status === "running" ? status : record.status, totalMs: record.totalMs } : null);
      setDeciding(false);
    };
    const failOpenNodes = (summary: string) => {
      pacer.reset(); // queued "running" changes must not land after the failure
      setView((v) => {
        const next = { ...v };
        for (const node of NODES) if (next[node.id].state === "running" || next[node.id].state === "queued") next[node.id] = { ...next[node.id], state: "failed", summary };
        return next;
      });
    };
    try {
      await streamRun(path, body, {
        onFirstByte: () => { window.clearTimeout(warmTimer); setWarming(false); },
        onEvent: (event, ms) => {
          if (event.type === "run_started") id = onStarted(event.run_id);
          if (!id) return;
          const runId = id;
          serverT.push(event.t);
          receivedT.push(ms);
          // The run's time so far, read before this event is stored: a decision adds to its question's time.
          const before = event.type === "run_finished" ? sessionRef.current.runs.find((run) => run.id === runId)?.totalMs ?? 0 : 0;
          appendEvent(runId, event, ms);
          if (event.type === "node") pacer.schedule(() => setView((v) => applyEvent(v, event)));
          else if (event.type === "result") {
            lastResult = event.result;
            pacer.schedule(() => {
              setShown(event.result);
              setArrival(event.result);
            });
          } else if (event.type === "error") pacer.schedule(() => setNotice({ tone: "error", text: capFirst(event.message) }));
          else if (event.type === "run_finished") {
            finished = true;
            const result = lastResult;
            const final: Paced = { status: event.status, totalMs: before + event.total_ms };
            pacer.schedule(() => {
              setPaced(final);
              setDeciding(false);
              setStatus(announcement(final.status, result));
            });
          }
        },
      }, controller.signal);
      if (!finished && id) {
        close("incomplete");
        failOpenNodes("the connection closed before this step finished");
        endEarly("incomplete");
        setNotice({ tone: "error", text: "The connection closed before the run finished. The book changed only if History shows this run as Applied." });
      } else if (id && wasBuffered(serverT, receivedT)) {
        const runId = id;
        setSession((s) => ({ ...s, runs: s.runs.map((run) => (run.id === runId ? { ...run, buffered: true } : run)) }));
        replayTimer.current = window.setTimeout(() => {
          replayTimer.current = null;
          const record = sessionRef.current.runs.find((run) => run.id === runId);
          if (record) replay(record, false);
        }, 50);
      }
    } catch (error) {
      if (error instanceof ApiError) setNotice({ tone: "error", text: capFirst(error.message), fields: error.fields });
      else if (error instanceof DOMException && error.name === "AbortError") setNotice({ tone: "error", text: `The server did not respond within ${RUN_TIMEOUT_MS / 1000} s. Steps still running are marked failed. Run the question again.` });
      else setNotice({ tone: "error", text: "Could not reach the server. Check the connection and run the question again." });
      failOpenNodes("the server did not respond");
      close("error");
      endEarly("error");
      setStatus("The run stopped: the server did not respond.");
    } finally {
      window.clearTimeout(timeout);
      window.clearTimeout(warmTimer);
      setWarming(false);
    }
  }, [appendEvent, pacer, replay]);

  const ask = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q || busy) return;
    setNotice(null);
    setBusy("question");
    setDrawer(null);
    setSelectedClient(null);
    setReplaying(false);
    setDeciding(false);
    setBandOpen(false);
    setResultTab("impact");
    fresh();
    setActiveId(null);
    setView(idleView());
    setShown(undefined);
    setPaced({ status: "running", totalMs: 0 });
    focusNext.current = "result";
    showRun.current = true;
    setStatus(`Running: ${q}`);
    const s = sessionRef.current;
    await runStream("/api/run", { question: q, history: s.history, materiality: s.materiality, used_ids: usedIds(s.audit) }, (runId) => {
      const record: RunRecord = { id: runId, question: q, at: new Date().toISOString(), events: [], receivedAt: [], status: "running", totalMs: 0 };
      setSession((current) => ({ ...current, runs: [...current.runs, record].slice(-30) }));
      setActiveId(runId);
      return runId;
    });
    setBusy(null);
  }, [busy, fresh, runStream]);

  const decide = useCallback(async (decision: "approve" | "reject", approver: string) => {
    const record = active;
    const preview = record?.preview;
    if (!record || !preview?.preview_token || !preview.change || busy) return;
    setNotice(null);
    setBusy("decision");
    setReplaying(false);
    setDeciding(true);
    setResultTab("impact");
    if (replayTimer.current !== null) window.clearTimeout(replayTimer.current);
    setPaced((p) => ({ status: "running", totalMs: p?.totalMs ?? 0 }));
    focusNext.current = "decision";
    setStatus(decision === "approve" ? "Applying the change." : "Turning the change down.");
    const s = sessionRef.current;
    const body: DecisionBody = {
      run_id: record.id, question: record.question, change: preview.change, history: s.history,
      materiality: preview.materiality ?? s.materiality, approver, decision, preview_token: preview.preview_token, hypothetical: !!preview.hypothetical,
    };
    await runStream("/api/approve", body, () => record.id);
    setBusy(null);
  }, [active, busy, runStream]);

  /** Clear the run on screen: what Reset and Undo leave behind, since it showed a session that no longer exists. */
  const clearScreen = useCallback(() => {
    fresh();
    setActiveId(null);
    setView(idleView());
    setShown(undefined);
    setArrival(null);
    setPaced(null);
    setDeciding(false);
    setReplaying(false);
    setSelectedClient(null);
  }, [fresh]);

  const doReset = useCallback(() => {
    setConfirm(null);
    clearScreen();
    setSession((s) => {
      const ids = s.history.map((item) => item.change.id);
      const detail = `${ids.length ? `undid ${ids.join(", ")}; ` : ""}the returns are back at rules version 1 and the original client facts${s.signoffs.length ? `; ${plural(s.signoffs.length, "sign-off")} cleared` : ""}`;
      return {
        ...s, history: [], checkpoints: [], signoffs: [], pack: undefined, facts: undefined, currentBook: undefined,
        audit: [...s.audit, resetEvent(detail, s.signer.trim(), { pack: basePack, materiality: s.materiality })],
      };
    });
    setNotice({ tone: "info", text: "The sample returns are back at rules version 1 and the original client facts. Run history and the audit log stay." });
    setStatus("The session is back at rules version 1.");
  }, [clearScreen, basePack]);

  const reset = useCallback(() => {
    const s = sessionRef.current;
    const items = [
      ...(s.history.length ? [`Undoes ${plural(s.history.length, "approved change")}: ${s.history.map((item, index) => {
        const at = clockOf(s.checkpoints[index]?.at);
        return `${item.change.id} (approved by ${item.approver}${at ? ` at ${at}` : ""})`;
      }).join(", ")}.`] : []),
      "The sample returns go back to rules version 1 and the original client facts.",
      ...(s.signoffs.length ? [`Clears ${plural(s.signoffs.length, "sign-off")} recorded on reached returns.`] : []),
      "Run history and the audit log stay; the audit log records the reset.",
    ];
    setConfirm({
      title: "Reset the session?",
      lead: s.history.length ? "Reset removes every approved change from this tab's session." : "No change is approved yet, so Reset only clears the screen.",
      items, confirm: "Reset the session", onConfirm: doReset,
    });
  }, [doReset]);

  const doUndo = useCallback(() => {
    setConfirm(null);
    const s = sessionRef.current;
    const last = s.history[s.history.length - 1];
    const point = s.checkpoints[s.checkpoints.length - 1];
    if (!last || !point || point.change_id !== last.change.id) return;
    const id = last.change.id;
    const closed = s.signoffs.filter((item) => item.change_id === id).length;
    const remaining = s.history.length - 1;
    const pack = remaining ? point.pack ?? basePack : basePack;
    clearScreen();
    setSession((current) => ({
      ...current,
      history: current.history.slice(0, -1),
      checkpoints: current.checkpoints.slice(0, -1),
      signoffs: current.signoffs.filter((item) => item.change_id !== id),
      pack: remaining ? point.pack : undefined,
      facts: remaining ? point.facts : undefined,
      currentBook: point.book,
      audit: [...current.audit, undoEvent(id, `removed from this session; the returns are back at rules version ${pack?.version ?? 1}${closed ? `; ${plural(closed, "sign-off")} closed` : ""}`,
        current.signer.trim(), { title: last.change.title, kind: last.change.kind, pack, materiality: last.materiality })],
    }));
    setNotice({ tone: "info", text: `${id} was undone. The sample returns are back at rules version ${pack?.version ?? 1}. Run ${id} again to preview it.` });
    setStatus(`${id} was undone.`);
    window.setTimeout(() => document.getElementById("main")?.focus({ preventScroll: true }), 0);
  }, [clearScreen, basePack]);

  const canUndo = session.history.length > 0 && session.checkpoints.length === session.history.length;
  const undo = useCallback(() => {
    const s = sessionRef.current;
    const last = s.history[s.history.length - 1];
    const point = s.checkpoints[s.checkpoints.length - 1];
    if (!last || !point) return;
    const closed = s.signoffs.filter((item) => item.change_id === last.change.id).length;
    const at = clockOf(point.at);
    const before = s.history.length > 1 ? point.pack ?? basePack : basePack;
    setConfirm({
      title: `Undo ${last.change.id}?`,
      lead: `Undo removes ${last.change.id} (${last.change.title}), approved by ${last.approver}${at ? ` at ${at}` : ""}, from this tab's session.`,
      items: [
        `The sample returns go back to rules version ${before?.version ?? 1} and the client facts they had before ${last.change.id}.`,
        ...(closed ? [`Closes ${plural(closed, "sign-off")} recorded on its returns.`] : []),
        "The audit log keeps every event and records the undo.",
      ],
      confirm: `Undo ${last.change.id}`,
      onConfirm: doUndo,
    });
  }, [doUndo, basePack]);

  const sign = useCallback((row: ImpactRow, decision: SignDecision) => {
    const changeId = shown?.change_view?.id ?? shown?.change?.id;
    const name = sessionRef.current.signer.trim();
    const role = roleFor(row.route);
    if (!changeId || !name || !role) return;
    const entry: SignOff = { at: localIso(), change_id: changeId, client: row.client, route: row.route, decision, name, role };
    const base = { title: shown?.change_view?.title ?? shown?.change?.title ?? "", kind: "sign-off", pack: shown?.pack_after, materiality: shown?.materiality };
    setSession((s) => ({ ...s, signoffs: [...s.signoffs, entry], audit: [...s.audit, signOffEvent(entry, base)] }));
    setStatus(`${row.client}: ${DECISION_WORDS[decision]} by ${name}, ${role}.`);
  }, [shown]);

  const changeMateriality = useCallback((value: string) => {
    setSession((s) => ({ ...s, materiality: value, audit: [...s.audit, materialityEvent(s.materiality, value, s.signer.trim(), s.pack ?? basePack)] }));
    setStatus(`The amend threshold is now ${dollars(value)}. It applies from the next preview.`);
  }, [basePack]);

  const openClient = useCallback((client: string, line?: string) => {
    setSelectedClient(client);
    setDrawer({ kind: "return", client, line });
  }, []);
  const onFigure = useCallback((link: Link) => { if (link.client) openClient(link.client, link.line); }, [openClient]);
  const openNode = useCallback((id: NodeId) => setDrawer({ kind: "node", id }), []);
  const openAbout = useCallback(() => setDrawer({ kind: "about" }), []);
  const openSettings = useCallback(() => setDrawer({ kind: "about", settings: true }), []);
  const run = useCallback((text: string) => { setQuestion(text); void ask(text); }, [ask]);
  // An example runs its own words and leaves the question box to the reviewer's: the question shows on the run.
  const runExample = useCallback((text: string) => { void ask(text); }, [ask]);
  // Run again keeps focus on the run: the button goes away while the steps run, so focus moves to their heading.
  const rerun = useCallback(() => {
    if (!active) return;
    const text = active.question;
    document.getElementById("graph-title")?.focus();
    run(text);
  }, [active, run]);
  const toggleSteps = useCallback(() => {
    writeFlag("steps-open", stepsOpen ? "0" : "1");
    setStepsOpen(!stepsOpen);
  }, [stepsOpen]);
  const toggleBand = useCallback(() => {
    writeFlag("band-hidden", bandOpen ? "1" : "0");
    setBandOpen(!bandOpen);
  }, [bandOpen]);
  const focusAnswer = useCallback(() => {
    const heading = document.getElementById("answer-title");
    heading?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    heading?.focus({ preventScroll: true });
  }, [reduced]);
  const goToApproval = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    document.getElementById("approval")?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    const input = document.getElementById("approver") as HTMLInputElement | null;
    (input && !input.disabled ? input : document.getElementById("gate-title"))?.focus({ preventScroll: true });
  }, [reduced]);

  // What the dashboard shows ------------------------------------------------------------------------------
  const questionChars = health?.limits?.question_chars ?? QUESTION_CHARS;
  const historyLimit = health?.limits?.history ?? HISTORY_LIMIT;
  const returns = book?.returns.length ?? health?.book?.returns ?? 40;
  const pack = session.history.length ? session.pack : basePack;
  const overtime = pack ? pack.overtime_label ?? (pack.overtime_definition === "flsa_premium" ? "only the FLSA premium (the extra half)" : "all overtime pay (W-2 box 14)") : "";
  const facts = session.history.length ? session.facts ?? {} : {};
  const changedFacts = Object.entries(facts);
  const running = pacedStatus === "running";
  const ended = pacedStatus !== null && !running;
  // A run still playing on screen counts as busy, so Run never looks ready while the steps say Running.
  const working = busy !== null || running;
  // The approval shows while its preview waits, and stays (busy) while the decision runs, until its result is drawn.
  const gateOpen = !!active?.preview && shown?.status === "waiting" && (pacedStatus === "waiting_for_approval" || deciding);
  const whatIf = shown?.status === "what_if" || (ended && active?.status === "what_if");
  const started = active?.events.find((e) => e.type === "run_started");
  const previewHistory = started?.type === "run_started" ? started.history.join(",") : "";
  const stale = gateOpen && previewHistory !== historyKey;
  const full = gateOpen && (!!active?.preview?.history_full || session.history.length >= historyLimit);
  const bookRows = (shown && PREVIEWS.includes(shown.status) ? shown.book : shown?.book ?? session.currentBook) ?? book?.returns ?? [];
  const isChange = shown?.kind === "change";
  const decision = decisionOf(active, shown);
  const shownChange = shown?.change_view?.id ?? shown?.change?.id;
  const latestChange = historyIds[historyIds.length - 1];
  // Undo is offered on the decision line of the session's latest change, and in History.
  const undoHere = shown?.status === "applied" && canUndo && shownChange === latestChange ? { id: latestChange, onUndo: undo } : null;
  // Sign-off is open on an applied change that is still in the session.
  const signControls: SignControls | undefined = shown?.status === "applied" && shownChange && historyIds.includes(shownChange) ? {
    changeId: shownChange,
    signer: session.signer,
    onSigner: (name: string) => setSession((s) => ({ ...s, signer: name })),
    latest: latestFor(session.signoffs, shownChange),
    onSign: sign,
  } : undefined;
  // The steps open while a question runs and fold before and after it, unless the reviewer keeps them open. A
  // decision runs with them folded, so the page stays where the reviewer approved.
  const graphOpen = (running && !deciding) || stepsOpen;
  const statusLabel = !shown ? (session.history.length ? `After ${plural(session.history.length, "approved change")}` : "Start of session, rules version 1")
    : shown.status === "waiting" ? "Preview"
    : shown.status === "what_if" ? "What-if"
    : shown.status === "applied" ? (shownChange && !historyIds.includes(shownChange) ? "Applied, then undone" : "Applied")
    : shown.status === "turned_down" ? "Turned down, nothing applied"
    : shown.status === "rejected" ? "Failed validation, nothing applied"
    : shown.status === "stopped" ? "Stopped, nothing applied"
    : shown.kind === "explain" ? "Current book" : "Nothing applied";
  const emptyImpact = !shown ? "Run one to see which returns it affects."
    : shown.status === "rejected" || shown.status === "stopped" ? "Nothing was applied. The answer lists the reasons."
    : shown.status === "turned_down" ? "Turned down, so nothing was applied. The answer says what the change would have done."
    : shown.kind === "explain" ? "A question about a client changes nothing. The answer shows the line and its inputs."
    : shown.kind === "unsupported" ? "Nothing ran. Try one of the examples."
    : shown.message ? "Nothing was applied."
    : "No return reached.";
  const notesEmpty = whatIf ? "A what-if writes no notes or letters."
    : shown?.status === "turned_down" ? "Turned down: no notes or letters are written."
    : "Each approved change writes an internal note for every reached return; a missing fact or a filed return that moves also writes a letter to the client.";
  const drawerRow = drawer?.kind === "return" ? shown?.impact?.find((row) => row.client === drawer.client) : undefined;
  const explained = drawer?.kind === "return" && shown?.kind === "explain" && !!shown.client && shown.client === drawer.client ? shown : undefined;
  const modal = drawer !== null || confirm !== null;

  const services = health?.services;
  const servicesNow = servicesSummary(health, apiDown !== null);
  const engineB = lastNode(active, "engine_b");
  const crossCheck = { label: crossCheckLabel(services?.policyengine, engineB), note: engineB?.fallback ? `This run: ${engineB.fallback}.` : null };
  const notesStep = lastNode(active, "notes");
  const notesModel = modelOf(notesStep, services?.anthropic.model);
  const wording: Wording = {
    by: notesModel.ollama ? `Ollama on this machine${notesModel.model ? ` (${notesModel.model})` : ""}` : `the Anthropic API${notesModel.model ? ` (${notesModel.model})` : ""}`,
    reason: notesStep?.fallback?.startsWith("template") ? notesStep.fallback.replace(/^templates?:\s*/, "") : "",
  };
  const answerBy = modelOf(lastNode(active, "explain")).ollama ? "Ollama on this machine" : "the Anthropic API";
  const caption = reduced
    ? "Reduced motion is on: steps update as the server reports them, with no animation. Times are the server's."
    : "Each box is one step of the run, with the time the server took.";
  const runText = !paced ? "No run yet"
    : running ? (replaying ? "Replaying the recorded run" : "Running")
    : summaryOf(paced.status, shown, false);
  const downloadRun = () => {
    if (active) saveFile(`taxdiff-run-${active.id}.json`, JSON.stringify({ question: active.question, at: active.at, status: active.status, result: active.result, events: active.events }, null, 2), "application/json");
  };
  const approved: ApprovedItem[] = session.history.map((item, index) => ({
    id: item.change.id, title: item.change.title, approver: item.approver, at: session.checkpoints[index]?.at ?? "",
  }));

  const mainExample = examples?.find((example) => example.id === "fix") ?? null;
  // The reach strip applies to a change that ran; a question about a client, an unread question or a failed change
  // set hides it. The impact list, notes and audit log are tabs under the result and say when they have nothing.
  const showReach = !shown || (isChange && Boolean(shown.kpis));
  // Panels that a run will refresh say so while it runs. A question run empties them until its result; while
  // a decision runs, the preview's figures stay in view.
  const pending = running;
  // Before the first run the book leads; the answer and reach panels appear with the first run.
  const atStart = !shown && !pending;
  // The guide: step 1 while the change is read and checked, 2 from the recompute on (and when a result needs no
  // decision), 3 while the approval waits or the decision runs; all three are done once the change is applied or
  // turned down.
  const decided = pacedStatus === "applied" || pacedStatus === "turned_down";
  const reaching = view.engine_a.state !== "idle" && view.engine_a.state !== "skipped";
  const guideStep = !paced ? 0
    : running ? (deciding ? 3 : reaching ? 2 : 1)
    : ENDED.includes(pacedStatus ?? "") ? (active?.preview ? 3 : 1)
    : pacedStatus === "waiting_for_approval" || decided ? 3
    : 2;
  // A result that arrived live on this screen enters once: squares and rows in the impact list's order.
  const arrive: Arrival | null = useMemo(
    () => (arrival && arrival === shown ? { result: arrival, delays: arrivalDelays(isChange ? arrival.impact ?? [] : []) } : null),
    [arrival, shown, isChange],
  );
  const story = shown?.story ?? null;
  const tabCounts = {
    impact: isChange ? shown?.impact?.length : undefined,
    notes: shown?.status === "applied" ? shown.notes?.length : undefined,
    audit: session.audit.length,
  };

  return (
    <>
      <div className="sr-only" role="status" aria-live="polite">{status}</div>
      <div className="app" inert={modal ? true : undefined}>
        <header className="topbar">
          <div className="brand">
            <Mark />
            <h1>TaxDiff</h1>
            <span className="tagline">Which prepared returns a rule or fact change reaches</span>
          </div>
          <div className="scope" role="group" aria-label="Sample returns and rules">
            <span className="synthetic" title={`All ${returns} returns and employer statements are made up; no taxpayer data is used.`}><span className="on-wide">SYNTHETIC DATA</span><span className="on-phone">SYNTHETIC</span></span>
            <span className="rules" title="The 2025 tax rules this session uses (the rule pack); each approved rule change makes the next version.">
              Rules: <b>{pack?.tax_year ?? book?.tax_year ?? 2025}, version {pack?.version ?? 1}</b>
            </span>
            <span className="ot" title={overtime ? `Qualified overtime: ${overtime}${pack?.overtime_definition === "flsa_premium" ? ". The FLSA premium is the extra half of the pay rate for overtime hours." : ""}` : undefined}>
              Overtime: <b>{overtime || "-"}</b>
            </span>
            {changedFacts.length ? <span className="facts">Facts: <b>{changedFacts.map(([c, v]) => `${c} v${v}`).join(", ")}</b></span> : null}
            <span className="year">{returns} sample returns</span>
          </div>
          <div className="bar-actions">
            <a className="services" href="/status" data-state={servicesNow.state} title={servicesNow.title}>
              <span className="dot" aria-hidden="true" /><span>Services<span className="services-state">: {servicesNow.text}</span></span>
            </a>
            <div className="actions">
              <button type="button" className="btn small ghost" onClick={() => theme.setChoice(theme.resolved === "dark" ? "light" : "dark")}
                      aria-label={`Switch to the ${theme.resolved === "dark" ? "light" : "dark"} theme`}>
                {theme.resolved === "dark" ? "Light" : "Dark"}
              </button>
              <button type="button" className="btn small ghost" onClick={openAbout}>About</button>
              <button type="button" className="btn small" onClick={reset} disabled={working}>Reset</button>
            </div>
          </div>
        </header>

        {apiDown ? <div className="banner error" role="alert">{apiDown} Runs need the server. <button type="button" className="link" onClick={() => void refreshHealth()}>Try again</button></div> : null}
        {warming ? <div className="banner" role="status">Starting the server. The first request after a quiet period starts a new server, which takes a few seconds.</div> : null}
        {saveFailed ? <div className="banner error" role="alert">This tab could not save the session. Approved changes stay while the page is open; a reload would lose them.</div> : null}

        <section className="problem" data-open={bandOpen} aria-labelledby="problem-title">
          <div className="problem-head">
            <h2 id="problem-title">Which prepared returns does a tax change affect?</h2>
            <button type="button" className="btn small ghost" aria-expanded={bandOpen} aria-controls="problem-body" onClick={toggleBand}>
              {bandOpen ? "Hide this" : "What this does"}
            </button>
          </div>
          <div className="problem-body" id="problem-body" hidden={!bandOpen}>
            <p className="lede">When a tax rule or a client&apos;s documents change after returns are prepared, TaxDiff finds every return it
              affects, shows how much each one moves, and says what to do next.</p>
            <div className="problem-actions">
              {mainExample ? (
                <button type="button" className="btn primary" disabled={working} onClick={() => runExample(mainExample.question)}
                        title={mainExample.question}>Run the main example</button>
              ) : null}
              <button type="button" className="link how-toggle" aria-expanded={howOpen} aria-controls="how-body" onClick={() => setHowOpen(!howOpen)}>
                How it works
              </button>
            </div>
            <div className="how" id="how-body" hidden={!howOpen}>
              <div className="how-text">
                <p>The main example: release 1 of the engine counted all W-2 box 14 overtime pay as qualified overtime. IRS Notice 2025-69
                  counts only the FLSA premium, the extra half of the pay rate for overtime hours. <span className="id">CS-001</span> corrects
                  the rule, and TaxDiff shows which of the {returns} sample returns move.</p>
                <p>Done by hand, a firm checks return by return, and a filed return that is missed is an amendment that is missed. Here every
                  number comes from code with its source, and nothing changes until a reviewer approves.</p>
              </div>
              <ol className="problem-steps">
                <li>Type a change or a question, change a client fact, paste IRS guidance, or pick an example.</li>
                <li>Each step runs on screen: the change is checked, the returns are recomputed by this project&apos;s engine and by
                  PolicyEngine-US, and each moved line is cited to its IRS or eCFR source.</li>
                <li>Every affected return gets its next step: update, back to the reviewer, amendment review, or a document request.
                  Each return it does not affect is listed with the reason.</li>
                <li>Nothing applies until a reviewer approves that exact preview; then the notes, the letters and the audit entry are
                  written, and each return is signed off by role.</li>
              </ol>
            </div>
          </div>
        </section>

        <div className="layout">
          <aside className="side" aria-label="Question and history">
            <SidePanel tab={tab} onTab={setTab} question={question} onQuestion={setQuestion} questionChars={questionChars}
                       busy={working} running={busy ?? (running ? "question" : null)} onSubmit={() => void ask(question)} onRun={runExample} onPreviewFacts={run}
                       notice={notice} materiality={session.materiality} onSettings={openSettings}
                       examples={examples} apiDown={apiDown !== null} historyIds={historyIds} book={book?.returns ?? []}
                       runs={session.runs} activeId={activeId} onReplay={replay} approved={approved} onUndo={canUndo ? undo : null} />
          </aside>

          <main className="main" id="main" tabIndex={-1}>
            <section className="panel graph-panel" id="steps" aria-labelledby="graph-title" data-open={graphOpen}
                     data-run-status={pacedStatus ?? "none"} data-shown-status={shown?.status ?? ""}>
              <div className="panel-head">
                <h2 id="graph-title" tabIndex={-1}>Run steps</h2>
                <span className="run-status">
                  <b>{runText}</b>
                  {/* Step counts and times belong to the open steps view; folded, the strip says only the outcome. */}
                  {ended && graphOpen ? <span className="run-facts"> · {stepCounts(view)}{paced && paced.totalMs ? ` · ${formatMs(paced.totalMs)} on the server` : ""}</span> : null}
                </span>
                <span className="end">
                  {replaying ? <span className="replay-flag">Replay of the recorded run, at server times</span> : null}
                  {active?.buffered ? <span className="replay-flag">Replayed with server times: the network delivered the events in one batch</span> : null}
                  {gateOpen && !deciding ? <a className="btn small" href="#approval" onClick={goToApproval}>Go to the approval</a> : null}
                  {active && ended && ENDED.includes(pacedStatus ?? "") ? <button type="button" className="btn small" onClick={rerun} disabled={busy !== null}>Run again</button> : null}
                  {!running ? (
                    <button type="button" className="btn small" aria-expanded={stepsOpen} aria-controls="graph-body" onClick={toggleSteps}>
                      {stepsOpen ? "Hide steps" : "Show steps"}
                    </button>
                  ) : null}
                </span>
              </div>
              {/* Open while a run streams; when it ends the graph folds into the strip above, and Show steps opens it again. */}
              <Fold id="graph-body" open={graphOpen} animate={motionReady && !reduced}>
                {(live) => (
                  <>
                    {active ? <p className="panel-sub run-question" title={active.question}>Question: {active.question}</p> : null}
                    {!live ? null : phone ? <Stepper view={view} selected={drawer?.kind === "node" ? drawer.id : null} onOpen={openNode} />
                      : <WorkflowGraph view={view} selected={drawer?.kind === "node" ? drawer.id : null} onOpen={openNode} theme={theme.resolved} />}
                    <div className="graph-foot">
                      <p className="graph-caption">{caption}</p>
                      {active && !running ? <button type="button" className="btn small" onClick={downloadRun}>Download JSON</button> : null}
                    </div>
                  </>
                )}
              </Fold>
            </section>

            {/* The result in the guide's order: what changes for one return, who the change reaches, the decision. */}
            <Guide step={guideStep} complete={decided} decision={guideStep !== 2} still={!motionReady} />
            {story ? <StoryCard story={story} arrive={arrive?.result ?? null} onClient={(client) => openClient(client)} /> : null}
            {whatIf ? <p className="whatif" role="note"><b>What-if:</b> nothing was applied. The rules and the returns are unchanged.</p> : null}
            {showReach && !atStart ? (
              <ReachStrip kpis={isChange ? shown?.kpis : undefined} rows={isChange ? shown?.impact : undefined} notReached={isChange ? shown?.not_reached : undefined}
                          book={bookRows} label={statusLabel} pending={pending} crossCheck={crossCheck} arrive={arrive} onClient={(client) => openClient(client)} />
            ) : null}
            {gateOpen && active?.preview ? (
              <ApprovalGate key={active.id} preview={active.preview} factsBefore={facts} stale={stale} full={full} limit={historyLimit} busy={busy !== null}
                            firmMateriality={session.materiality} onDecide={(d, name) => void decide(d, name)} onReset={reset}
                            onClient={(client) => openClient(client)} onSettings={openSettings} />
            ) : null}
            {!gateOpen && decision ? (
              <DecisionLine text={decision} outcome={shown?.status === "turned_down" ? "turned_down" : "applied"} arrive={arrive?.result ?? null}
                            onAnswer={focusAnswer} undo={undoHere} />
            ) : null}
            {gateOpen || atStart ? null : (
              <AnswerCard result={shown} question={active?.question} pending={pending} wordedBy={answerBy} onFigure={onFigure} onRerun={rerun} onReset={reset} onAsk={run}
                          brief={shown?.status === "applied" || shown?.status === "turned_down"} />
            )}

            {/* One of three at a time under the result; each keeps its section, heading and id. */}
            <ResultTabs tab={resultTab} onTab={setResultTab} counts={tabCounts} panels={{
              impact: (
                <ImpactPanel rows={isChange ? shown?.impact : undefined} label={statusLabel} selected={selectedClient} pending={pending}
                             changeId={shownChange} onRow={(client) => openClient(client)} empty={emptyImpact}
                             book={shown ? undefined : bookRows} sign={signControls} arrive={arrive} />
              ),
              notes: <NotesPanel notes={shown?.status === "applied" ? shown.notes : undefined} empty={notesEmpty} pending={pending} wording={wording} />,
              audit: <AuditPanel events={session.audit} />,
            }} />
          </main>
        </div>

        <footer className="footer">
          TaxDiff{health?.version ? ` ${health.version}` : ""} · tax year {book?.tax_year ?? 2025}
          {services?.policyengine ? ` · Cross-check: ${policyEngineLabel(services.policyengine)} (open-source tax model)` : ""}
        </footer>
      </div>

      {drawer?.kind === "node" ? <NodeDrawer id={drawer.id} view={view[drawer.id]} onClose={() => setDrawer(null)} /> : null}
      {drawer?.kind === "return" ? <ReturnDrawer client={drawer.client} row={drawerRow} book={bookRows.find((r) => r.client === drawer.client)}
                                                 unreached={shown?.not_reached?.find((r) => r.client === drawer.client)}
                                                 chain={explained?.chain} computation={explained}
                                                 chainLabel={explained?.line_label ? lowerFirst(lineName(explained.line_label)) : undefined}
                                                 onClose={() => setDrawer(null)} /> : null}
      {drawer?.kind === "about" ? <AboutDrawer health={health} materiality={session.materiality} onMateriality={changeMateriality}
                                               focusSettings={drawer.settings} onClose={() => setDrawer(null)} /> : null}
      {confirm ? <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} /> : null}
    </>
  );
}
