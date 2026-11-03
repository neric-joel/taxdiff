"""The two streamed runs behind the dashboard: a question, and the reviewer's decision on a preview.

A question runs Question, Intake, then either the change pipeline (Documents, Validate, Engine A, Engine B
and Law, Route, a stop at Approve with the preview, and the Audit log's preview entry) or, for a question
about a client, a lookup in the replayed session and the Explain node. A decision recomputes the preview,
checks it against the signed preview token, then approves it (Notes, Explain, Audit log) or turns it down
(Explain, Audit log).

Every node event comes from the workflow's own callbacks or from a step this module runs and times. A node
that does not run is reported as skipped with the reason; one that was running when the run stopped is
reported as failed with its elapsed time. The events go into a queue on a worker thread and the response
streams them as NDJSON, one event per line. When the client goes away, the worker stops at its next step.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from queue import Empty, Queue
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..changes import ChangeSet
from ..engine import LINES, extremes
from ..facts import fact_document, fact_label
from ..lanes import explain, law, llm, questions
from ..lanes import notes as notes_lane
from ..paths import ROOT
from ..router import ROUTES
from ..rules import LABELS, is_hypothetical
from ..workflow import (
    ENGINE,
    AuditEvent,
    LaneResult,
    approve_run,
    record_intake_refusal,
    record_rejection,
    run_change,
    run_json,
)
from . import payloads, signing
from .session import (
    PARITY_MODE,
    Approved,
    HistoryError,
    Session,
    Sources,
    check_change,
    next_change_id,
    receipt_for,
    replay,
    tip,
)

LAW_MODE: Literal["cache", "live"] = "cache" if os.environ.get("TAXDIFF_LAW", "live") == "cache" else "live"
DOCLING_LIVE = os.environ.get("TAXDIFF_DOCLING", "cache") == "live"
NOTES_WORDING = os.environ.get("TAXDIFF_NOTES", "llm") != "template"
RUN_DEADLINE = float(os.environ.get("TAXDIFF_RUN_DEADLINE", "90"))  # seconds before the stream gives up; the browser waits 100
MAX_HISTORY = 25  # approved changes a session can carry; each request replays them all
QUESTION_CHARS = 2000  # room for a pasted paragraph of guidance

SOURCE_OF = {"cache": "cached", "live": "live", "api": "live"}  # Engine B's mode as the node's source
NODES = ("question", "intake", "documents", "validate", "engine_a", "engine_b", "law", "route", "approve", "notes", "explain", "audit")
PROVIDER = {
    "documents": "pattern map",
    "validate": "schema and rule pack checks",
    "engine_a": "this project's engine",
    "route": "routing table",
}


def _cases() -> dict[str, str]:
    try:
        return json.loads((ROOT / "data" / "coverage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CASES = _cases()


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=QUESTION_CHARS)
    history: list[Approved] = Field(default_factory=list, max_length=MAX_HISTORY)
    materiality: Decimal = Field(default=Decimal(50), ge=0, le=1_000_000, decimal_places=2)
    used_ids: int = Field(default=0, ge=0, le=899)  # change ids this session has shown, so a new preview gets a fresh one


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default="", max_length=40)
    question: str = Field(default="", max_length=QUESTION_CHARS)
    change: ChangeSet
    history: list[Approved] = Field(default_factory=list, max_length=MAX_HISTORY)
    materiality: Decimal = Field(default=Decimal(50), ge=0, le=1_000_000, decimal_places=2)
    approver: str = Field(default="", max_length=80)
    decision: Literal["approve", "reject"]
    preview_token: str = Field(min_length=8, max_length=64)
    hypothetical: bool = False


@dataclass(frozen=True)
class Models:
    """Which LLM provider this request may use, and why not when it may not."""

    provider: str | None
    why_not: str = ""


class Cancelled(Exception):
    """The client went away; the worker stops at its next step."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Stream:
    """One run's events. A worker thread fills it; the response drains it."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.t0 = perf_counter()
        self.queue: Queue[dict[str, Any] | None] = Queue()
        self.started: dict[str, tuple[float, str]] = {}
        self.states: dict[str, str] = {}
        self.closed = False
        self.ended = False
        self.lock = threading.Lock()

    def ms(self) -> float:
        return round((perf_counter() - self.t0) * 1000, 1)

    def check(self) -> None:
        if self.closed:
            raise Cancelled()

    def put(self, event: dict[str, Any]) -> None:
        with self.lock:
            if self.ended:
                return
            self.queue.put({"run_id": self.run_id, "t": self.ms(), **event})

    def _node(self, node: str, state: str, **fields: Any) -> None:
        self.states[node] = state
        started = self.started.get(node)
        self.put({"type": "node", "node": node, "state": state, "started_at": started[1] if started else None, **fields})

    def start(self, node: str, summary: str = "") -> None:
        self.started[node] = (perf_counter(), _now())
        self._node(node, "running", summary=summary)

    def queued(self, node: str, summary: str) -> None:
        self._node(node, "queued", summary=summary)

    def finish(self, node: str, state: str, summary: str, *, duration_ms: float | None = None, source: str | None = "live",
               provider: str = "", input: Any = None, output: Any = None, request: Any = None, error: str | None = None,
               fallback: str | None = None, **extra: Any) -> None:
        started = self.started.get(node)
        if duration_ms is None:
            duration_ms = (perf_counter() - started[0]) * 1000 if started else 0.0
        if started is None:
            self.started[node] = (perf_counter(), _now())
        self._node(node, state, summary=summary, duration_ms=round(duration_ms, 2), source=source, provider=provider,
                   input=input, output=output, request=request, error=error, fallback=fallback or None, **extra)

    def skip(self, node: str, summary: str) -> None:
        self.started.setdefault(node, (perf_counter(), _now()))
        self._node(node, "skipped", summary=summary, duration_ms=0.0, source=None, provider="", input=None, output=None,
                   request=None, error=None, fallback=None)

    def skip_rest(self, nodes: tuple[str, ...], summary: str) -> None:
        for node in nodes:
            if self.states.get(node) in (None, "queued"):
                self.skip(node, summary)

    def stop_open(self, reason: str) -> None:
        """End every node still open: one that was running fails with its elapsed time, one never started is skipped."""
        for node in NODES:
            state = self.states.get(node)
            if state == "running":
                self.finish(node, "failed", reason, source=None, error=reason)
            elif state == "queued":
                self.skip(node, f"not run: {reason}")

    def result(self, result: dict[str, Any]) -> None:
        self.put({"type": "result", "result": result})

    def finished(self, status: str) -> None:
        self.put({"type": "run_finished", "status": status, "total_ms": self.ms()})
        with self.lock:
            self.ended = True
            self.queue.put(None)

    def lines(self) -> Iterator[str]:
        deadline = perf_counter() + RUN_DEADLINE
        try:
            while True:
                remaining = deadline - perf_counter()
                try:
                    event = self.queue.get(timeout=max(remaining, 0.01))
                except Empty:
                    reason = f"no answer within {RUN_DEADLINE:.0f} s"
                    self.stop_open(reason)
                    self.put({"type": "error", "message": f"the run took longer than {RUN_DEADLINE:.0f} s and was stopped"})
                    self.finished("error")
                    while True:
                        late = self.queue.get()
                        if late is None:
                            return
                        yield json.dumps(late, default=str) + "\n"
                if event is None:
                    return
                yield json.dumps(event, default=str) + "\n"
        finally:
            self.closed = True  # the client went away or the stream ended: the worker stops at its next step


def _metered(fn: Callable[[], Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Run fn and return what it returned and the LLM calls it made (provider, model, tokens, time)."""
    calls: list[dict[str, Any]] = []
    token = llm.METER.set(calls)
    try:
        return fn(), calls
    finally:
        llm.METER.reset(token)


def _request(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not calls:
        return None
    return {"calls": calls, "tokens_in": sum(c.get("tokens_in") or 0 for c in calls), "tokens_out": sum(c.get("tokens_out") or 0 for c in calls)}


def _error_event(question: str, detail: str) -> dict[str, Any]:
    event = AuditEvent(at=datetime.now().isoformat(timespec="seconds"), change_id="-", title=question[:120], kind="question",
                       author="web API", outcome="error", detail=detail, pack_version=0, engine=ENGINE)
    return asdict(event)


def _spawn(stream: Stream, question: str, work: Callable[[], None], on_done: Callable[[], None] | None) -> Stream:
    def target() -> None:
        llm.GUARD.set(llm.Guard(deadline=stream.t0 + RUN_DEADLINE - 5, stopped=lambda: stream.closed))
        try:
            work()
        except Cancelled:
            stream.finished("cancelled")
        except Exception as exc:  # report it on the stream; the run never ends silently, and no internal text leaks
            detail = f"the run stopped on an unexpected error ({type(exc).__name__}); nothing was applied"
            stream.put({"type": "error", "message": detail})
            stream.stop_open(detail)
            stream.skip_rest(NODES[:-1], "not run: the run stopped on an error")
            stream.start("audit")
            event = _error_event(question, detail)
            stream.finish("audit", "done", f"1 event recorded: error ({type(exc).__name__})",
                          provider="audit log (kept in this tab)", output=[event])
            stream.result({"kind": "error", "status": "error", "message": detail, "audit": [event]})
            stream.finished("error")
        finally:
            if on_done is not None:
                on_done()

    threading.Thread(target=target, daemon=True).start()
    return stream


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# Workflow callbacks -> node events --------------------------------------------------------------------


def _lane_source(lane: LaneResult) -> tuple[str | None, str, str | None]:
    """(source, provider, fallback) for a lane the workflow reported."""
    detail = lane.detail or {}
    if lane.lane == "documents":
        cached = "cached conversion" in lane.summary
        return ("cached" if cached else "live"), ("Docling (cached conversion), then pattern map" if cached else "pattern map"), None
    if lane.lane == "engine_b":
        output = detail.get("output")
        source = getattr(output, "source", "")
        version = getattr(output, "version", "")
        provider = "PolicyEngine API (api.policyengine.org)" if source == "api" else f"PolicyEngine-US {version}".strip()
        fallback = getattr(output, "fallback", "") or None
        if source == "failed" or lane.state == "failed":
            return "fallback", provider, "PolicyEngine-US could not run; returns route by status alone"
        if lane.state == "skipped" and source in ("cache", "live", "api"):
            return SOURCE_OF[source], provider, fallback or "no PolicyEngine-US result for these inputs; the reached returns route by status alone"
        if getattr(output, "reused", False) and not fallback:
            return "cached", provider, None  # an earlier identical request's answer, kept in memory on this server
        return ("fallback" if fallback else SOURCE_OF.get(source)), provider, fallback
    if lane.lane == "law":
        items = detail.get("authorities") or []
        if any(a.fetched for a in items):
            return "live", "eCFR API (ecfr.gov)", None
        missed = next((a.fallback for a in items if a.fallback), "")
        if any(a.excerpt for a in items):
            return "cached", "saved eCFR excerpts", missed or None
        return "live", "citation list (no eCFR section applies)", None
    return "live", PROVIDER.get(lane.lane, lane.lane), None


def _workflow_events(stream: Stream, change: ChangeSet, session: Session, *, what_if: bool = False) -> tuple[Callable[[LaneResult], None], Callable[[str], None]]:
    def on_start(name: str) -> None:
        stream.check()
        stream.start(name)
        if name == "engine_a":
            for parallel in ("engine_b", "law"):
                stream.queued(parallel, "waits for Engine A")

    def on_lane(lane: LaneResult) -> None:
        if lane.lane == "intake":
            return  # reported by the intake step itself
        if lane.state == "skipped" and lane.ms == 0.0:
            stream.skip(lane.lane, lane.summary)  # never started: the change stopped before it
            return
        source, provider, fallback = _lane_source(lane)
        data_in, data_out = payloads.lane_payload(lane.lane, lane.detail, change=change, state=session.state)
        request = None
        if lane.lane == "law":
            items = (lane.detail or {}).get("authorities") or []
            request = {"requests": [a.request for a in items if a.request]} or None
        if lane.lane == "approve":
            if what_if:
                stream.skip("approve", "a what-if is a preview only; nothing is applied")
            else:
                stream.finish("approve", "waiting", lane.summary, duration_ms=lane.ms, source=None, provider="reviewer", output=data_out)
            return
        stream.finish(lane.lane, lane.state, lane.summary, duration_ms=lane.ms, source=source, provider=provider,
                      input=data_in, output=data_out, request=request, fallback=fallback,
                      error=lane.summary if lane.state == "failed" else None)

    return on_lane, on_start


# Answers and the audit node -----------------------------------------------------------------------------


def _answer(stream: Stream, build: Callable[[], explain.Answer], question: str, data: Any) -> explain.Answer:
    """Run the Answer node: code writes the answer from the run, every figure inserted, and the headline is
    checked against the run's JSON. No model rewords it: changing an outcome or a direction ("was not run",
    "went up") would change its meaning without changing a figure."""
    stream.check()
    stream.start("explain", "writing the answer from the run")
    answer = replace(build(), source="template")
    outside = explain.figures_outside(answer.headline, data)
    stream.finish("explain", "done", "answer written by code from the run", source="live", provider="code (figures inserted from the run)",
                  input={"question": question}, output={"headline": answer.headline, "text": answer.text, "figures_not_in_run": outside})
    return answer


def _audit_node(stream: Stream, events: list[dict[str, Any]], note: str = "") -> None:
    stream.start("audit")
    outcomes = ", ".join(f"{e['change_id']} {e['outcome']}" + (f" by {e['approver']}" if e.get("approver") else "") for e in events)
    summary = f"{len(events)} {'event' if len(events) == 1 else 'events'} recorded: {outcomes}" if events else "nothing to record"
    stream.finish("audit", "done", summary + (f"; {note}" if note else ""), provider="audit log (kept in this tab)", output=events)


# The question run --------------------------------------------------------------------------------------


def _replay_or_fail(stream: Stream, history: list[Approved], node: str) -> Session | None:
    try:
        return replay(history)
    except HistoryError as exc:
        stream.finish(node, "failed", str(exc), provider="session replay", error=str(exc))
        stream.skip_rest(NODES, "not run: the session could not be rebuilt")
        stream.result({"kind": "error", "status": "error", "message": str(exc), "history_error": {"index": exc.index, "change": exc.change_id}})
        stream.finished("error")
        return None


def question(req: RunRequest, models: Models, run_id: str | None = None, on_done: Callable[[], None] | None = None) -> Stream:
    stream = Stream(run_id or new_run_id())
    return _spawn(stream, req.question, lambda: _question(stream, req, models), on_done)


def _question(stream: Stream, req: RunRequest, models: Models) -> None:
    stream.put({"type": "run_started", "mode": "question", "question": req.question, "at": _now(),
                "history": [item.change.id for item in req.history], "engine": __version__})
    stream.start("question", "rebuilding the session from the approved changes")
    session = _replay_or_fail(stream, req.history, "question")
    if session is None:
        return
    ids = [run.change.id for run in session.runs]
    stream.finish("question", "done",
                  f"rebuilt the session from {len(ids)} approved {'change' if len(ids) == 1 else 'changes'} ({', '.join(ids)}) "
                  f"in {session.ms:.1f} ms; receipts checked" if ids
                  else "session at rule pack v1 and the original facts",
                  duration_ms=session.ms, provider="session replay (Engine A, deterministic)",
                  input={"question": req.question, "approved": ids, "materiality": str(req.materiality)},
                  output={"pack": payloads.pack_view(session.state.pack), "facts_versions": payloads.facts_versions(session.state.book)})

    stream.check()
    stream.start("intake", "reading the question")
    started = perf_counter()
    change_id = next_change_id(req.history, req.used_ids)
    reading, calls = _metered(lambda: questions.read(req.question, change_id, session.state.book, models.provider, why_not=models.why_not,
                                                     pack=session.state.pack))
    intake_ms = (perf_counter() - started) * 1000
    model_read = bool(models.provider) and reading.method == llm.PROVIDERS.get(models.provider or "") and not reading.fallback
    if reading.kind == "change":
        assert reading.change is not None
        what = "a what-if" if reading.hypothetical else f"{reading.change.kind} change"
        summary = f"read as {what} {reading.change.id}: {reading.note or reading.change.title}"
    elif reading.kind == "explain":
        summary = f"read as a question about {', '.join(reading.clients)}: {LINES[reading.line].split(',')[0].lower()}"
    elif reading.kind == "session":
        summary = f"read as a question about this session: {reading.note}"
    else:
        summary = f"not run: {reading.reason}"
    reading_json = {"kind": reading.kind, "method": reading.method, "note": reading.note, "clients": list(reading.clients),
                    "line": reading.line, "direction": reading.direction, "hypothetical": reading.hypothetical, "reason": reading.reason,
                    "topic": reading.topic,
                    "change": reading.change.model_dump(mode="json", exclude_defaults=True) if reading.change else None}
    stream.finish("intake", "done", summary, duration_ms=intake_ms, source="fallback" if reading.fallback else "live",
                  provider=reading.method if not model_read else f"{reading.method}, checked against the text",
                  input={"text": req.question}, output=reading_json, request=_request(calls), fallback=reading.fallback or None)

    if reading.kind == "unsupported":
        stream.skip_rest(NODES[2:10], "not run: the question was not read as a change or a question about a client")
        reason = reading.reason + (f" ({reading.fallback})" if reading.fallback else "")
        answer = _answer(stream, lambda: explain.unsupported_answer(reason, questions.CAN_DO), req.question,
                         {"can_do": questions.CAN_DO, "reason": reason})
        refused = record_intake_refusal(session.state, change_id, req.question, reading.method, reading.reason)
        events = payloads.audit(refused.audit[session.audit_start:])
        _audit_node(stream, events)
        stream.result({"kind": "unsupported", "status": "unsupported", "question": req.question, "reading": reading_json,
                       "reason": reason, "can_do": list(questions.CAN_DO), "answer": payloads.answer(answer), "audit": events,
                       "book": payloads.book_rows(session.state, {}, CASES)})
        stream.finished("unsupported")
        return
    if reading.kind == "explain":
        _explain_client(stream, req, models, session, reading, reading_json)
        return
    if reading.kind == "session":
        _explain_session(stream, req, models, session, reading, reading_json)
        return
    assert reading.change is not None
    _preview(stream, req, models, session, reading, reading_json, intake_ms)


def _preview(stream: Stream, req: RunRequest, models: Models, session: Session, reading: questions.Reading,
             reading_json: dict[str, Any], intake_ms: float) -> None:
    change = reading.change
    assert change is not None
    problems = check_change(change)
    if problems:
        raise ValueError("; ".join(problems))
    stream.check()
    if not reading.hypothetical and any(is_hypothetical(edit.cite) for edit in change.rule_edits):
        reading = replace(reading, hypothetical=True)  # a change that cites no law, only a hypothesis, is a what-if
        reading_json = {**reading_json, "hypothetical": True}
    on_lane, on_start = _workflow_events(stream, change, session, what_if=reading.hypothetical)
    intake_lane = LaneResult("intake", "done", intake_ms, reading.note or change.title)
    run = run_change(session.state, change, materiality=req.materiality, parity_mode=PARITY_MODE, intake=intake_lane,
                     docling_live=DOCLING_LIVE, on_lane=on_lane, on_start=on_start, law_mode=LAW_MODE)
    new_events = payloads.audit(run.state.audit[session.audit_start:]) if run.state else []
    base = {"kind": "change", "question": req.question, "reading": reading_json, "materiality": str(req.materiality),
            "change": change.model_dump(mode="json"), "hypothetical": reading.hypothetical,
            "change_view": payloads.change_view(run.change, session.state.pack, session.state.book, run.citations),
            "pack_before": payloads.pack_view(session.state.pack), "cli": run_json(run)}

    if run.pending is None:  # rejected by Documents or Validate, or stopped after Engine A
        stopped = run.report is not None
        stream.skip_rest(NODES[2:10], "not run: the change was stopped" if stopped else "not run: the change was rejected")
        answer = _answer(stream, lambda: explain.rejected_answer(run.change, payloads.error_items(run.validation), session.state.pack.version,
                                                                 stopped=stopped),
                         req.question, {"errors": payloads.errors(run.validation), "pack": session.state.pack.version, "change": change.id})
        _audit_node(stream, new_events)
        status = "stopped" if stopped else "rejected"
        stream.result({**base, "status": status, "errors": payloads.errors(run.validation), "answer": payloads.answer(answer),
                       "audit": new_events, "book": payloads.book_rows(session.state, {}, CASES)})
        stream.finished(status)
        return

    pending, report = run.pending, run.report
    assert report is not None and run.validation is not None
    sources = Sources(dict(session.sources.rules), dict(session.sources.facts))
    sources.record(run)
    reach = {
        "kpis": payloads.kpis(report, pending.parity),
        "impact": [payloads.impact_item(item, run.change, book=pending.book, pack=pending.pack, parity=pending.parity,
                                        sources=sources, touched=run.validation.touched, materiality=req.materiality) for item in report.reached],
        "not_reached": payloads.not_reached_rows(report, session.state.results, run.validation.touched),
        "authorities": payloads.authorities(run.authorities),
        "book": payloads.book_rows(pending, {item.client_id: item.route for item in report.reached}, CASES),
        "audit": new_events,
    }
    if reading.hypothetical:
        # A what-if shows what a change would do and stops there: no token is signed, so it cannot be approved.
        stream.skip("notes", "a what-if writes no notes")
        answer = _answer(stream, lambda: explain.change_answer(run.change, report, session.state.pack,
                                                               payloads.facts_before(run.change, session.state.book),
                                                               decision="what-if", hypothetical=True),
                         req.question, {"kpis": reach["kpis"], "cli": run_json(run), "change": base["change_view"]})
        _audit_node(stream, new_events, "a what-if is never applied")
        stream.result({**base, **reach, "status": "what_if", "answer": payloads.answer(answer),
                       "story": payloads.story(report, run.change, run.validation.touched)})
        stream.finished("what_if")
        return
    _audit_node(stream, new_events, "your decision adds the next event")
    token = signing.sign("preview", digest=signing.preview_digest(run, req.materiality), tip=tip(req.history), hypothetical=False)
    stream.result({
        **base,
        **reach,
        "status": "waiting",
        "preview_token": token,
        "history_full": len(req.history) >= MAX_HISTORY,
        "pack_after": payloads.pack_view(pending.pack),
        "facts_after": payloads.facts_versions(pending.book),
        "story": payloads.story(report, run.change, run.validation.touched),
        # the letters approval would write, drafted now so the reviewer reads them before deciding
        "letters": [{"client": item.client_id, "kind": kind, "route": item.route, "text": text, "source": "template (letters are never reworded)"}
                    for item in report.reached for kind, text in notes_lane.letters(item, pending.book.tax_year, req.materiality)],
        "answer": None,
    })
    stream.finished("waiting_for_approval")


def _reads_text(result: Any, line: str, ret: Any, pack: Any, sources: Sources, touched: set[str]) -> list[tuple[str, str, str, str]]:
    """What a line reads, as (kind, name, label, value): reads an approved change touched first, then the
    line's own reads (not those of the earlier lines it used), facts with a value before empty ones."""
    used = result.uses.get(line, ())
    inherited = set().union(*(result.reads[u] for u in used)) if used else set()

    def rank(path: str) -> tuple[int, int, int, str]:
        kind, _, name = path.partition(":")
        first = 0 if path in touched else 1
        own = 0 if path not in inherited else 1
        if kind == "fact":
            if name in ("people", "filing_status") or name.endswith(("valid_ssn", "ssn_last4")):
                return first, own, 7, path
            return first, own, (1 if payloads.fact_value(ret, name) not in (None, False) else 6), path
        if kind == "rule":
            return first, own, (2 if name.endswith("definition") else 3 if ".cap" in name else 5), path
        return first, own, 8, path

    out = []
    for path in sorted(result.reads[line], key=rank):
        kind, _, name = path.partition(":")
        if path in inherited and path not in touched:
            continue  # read by an earlier line: the inputs sentence covers it
        if kind == "rule":
            if name.startswith(("brackets.", "tax_table.")) and path not in touched:
                continue  # the rate schedule and the Tax Table limit are in the chain view
            shown = "the rate schedule in the pack" if name.startswith(("brackets.", "tax_table.")) else explain.rule_value(pack, name)
            edited = f", set by {sources.rules[name]['change']}" if name in sources.rules else ""
            out.append(("rule", name, LABELS.get(name, name)[:1].upper() + LABELS.get(name, name)[1:], shown + edited))
        elif kind == "fact":
            value = payloads.fact_value(ret, name)
            source = sources.facts.get((result.client_id, name))
            origin = (f"from {source['change']}" + (", employer statement" if source.get("document") else "")) if source else f"{fact_document(name)}, client facts version {ret.facts_version}"
            label = fact_label(name)
            out.append(("fact", name, label[:1].upper() + label[1:], f"{explain.fact_text(name, value)} ({origin})"))
    return out


def _explain_client(stream: Stream, req: RunRequest, models: Models, session: Session, reading: questions.Reading,
                    reading_json: dict[str, Any]) -> None:
    client, line = reading.clients[0], reading.line
    state = session.state
    for node, why in (("documents", "a question reads no document"), ("validate", "a question changes nothing, so there is nothing to validate")):
        stream.skip(node, why)

    stream.start("engine_a", f"looking up {client} in the replayed session")
    now, start = state.results[client], session.initial.results[client]
    ret = state.book.get(client)
    assert ret is not None
    moves = [(run.change, item) for run in session.runs if run.report for item in run.report.reached
             if item.client_id == client and any(d.line == line for d in item.diffs)]
    reached_by = [run.change.id for run in session.runs if run.report and any(i.client_id == client for i in run.report.reached)]
    touched = {path for run in session.runs if run.validation for c, path in run.validation.touched if c in (None, client)}
    reads = _reads_text(now, line, ret, state.pack, session.sources, touched)
    chain = payloads.chain(now, line, ret, state.pack, session.sources, touched)
    stream.finish("engine_a", "done",
                  f"{client} now: {LINES[line].split(',')[0].lower()} {payloads.amount(now.values[line])['text']}; "
                  f"{len(now.reads[line])} recorded inputs; {len(reached_by)} approved {'change' if len(reached_by) == 1 else 'changes'} reached it",
                  provider="Engine A results from the replayed session", input={"client": client, "line": line},
                  output={"value": payloads.amount(now.values[line]), "reads": len(now.reads[line]), "reached_by": reached_by})

    stream.start("engine_b")
    parity = state.parity[client]
    stream.finish("engine_b", "done", f"PolicyEngine-US on {client}: {parity.state.replace('_', ' ')}" + (f" ({parity.reason})" if parity.reason else ""),
                  source=SOURCE_OF.get(PARITY_MODE), provider="PolicyEngine-US (from the session's Engine B results)",
                  output=payloads.parity_view(parity))

    stream.check()
    stream.start("law")
    lines = [line] + [d.line for _, item in moves for d in item.diffs]
    authorities = law.for_lines(lines, live=LAW_MODE == "live")
    fetched = [a for a in authorities if a.fetched]
    missed = next((a.fallback for a in authorities if a.fallback), None)
    stream.finish("law", "done", f"{len(authorities)} {'authority' if len(authorities) == 1 else 'authorities'} for the {LINES[line].split(',')[0].lower()} line"
                  + ("; FLSA excerpts fetched live from the eCFR API" if fetched else ""),
                  source="live" if fetched or not any(a.excerpt for a in authorities) else "cached",
                  provider="eCFR API (ecfr.gov)" if fetched else "citation list", fallback=missed,
                  output=[{"title": a.title, "url": a.url, "fetched": a.fetched} for a in authorities],
                  request={"requests": [a.request for a in authorities if a.request]} if fetched else None)

    stream.start("route")
    last = next(((run.change, item) for run in reversed(session.runs) if run.report for item in run.report.reached if item.client_id == client), None)
    summary = (f"{client}: routed to {ROUTES[last[1].route]} by {last[0].id}" if last else f"{client} has not been routed: no approved change reached it")
    stream.finish("route", "done", summary, provider=PROVIDER["route"], output={"route": last[1].route if last else None, "change": last[0].id if last else None})

    stream.skip("approve", "a question changes nothing, so there is nothing to approve")
    stream.skip("notes", "a question writes no notes")
    data = {"client": client, "now": payloads.amount(now.values[line]), "start": payloads.amount(start.values[line]),
            "chain": chain, "moves": [{"change": c.id, "title": c.title, "lines": [{"before": payloads.amount(d.before), "after": payloads.amount(d.after),
                                                                                     "delta": payloads.amount(d.delta, sign=True)} for d in item.diffs],
                                        "tax_change": payloads.tax_change_of(item), "vs_filed": payloads.vs_filed_of(item)} for c, item in moves],
            "as_filed": {k: str(v) for k, v in state.as_filed.get(client, {}).items()}, "reads": [f"{label}: {value}" for _, _, label, value in reads],
            "deltas": [str(payloads.delta_of(start.values[line], now.values[line]))] + [str(payloads.delta_of(v, now.values[line])) for k, v in state.as_filed.get(client, {}).items() if k == line],
            "facts_versions": {client: ret.facts_version}}
    inputs = [(LINES[u].split(",")[0].lower(), payloads.amount(now.values[u])["text"], u)
              for u in sorted(now.uses.get(line, ()), key=list(LINES).index) if u not in ("magi", "tax_formula")]
    answer = _answer(stream, lambda: explain.client_answer(client, line, reading.direction, start=start, now=now, moves=moves,
                                                           reached_by=reached_by, reads=reads, as_filed=state.as_filed.get(client),
                                                           filing_status=now.filing_status, inputs=inputs,
                                                           ends=extremes(ret, state.pack) if now.needs else None),
                     req.question, data)
    stream.skip("audit", "a question about a client changes nothing, so nothing is recorded")
    sheet_lines = [line] if line in payloads.SCHEDULE_1A else [name for name in payloads.SCHEDULE_1A if now.work.get(name)]
    stream.result({"kind": "explain", "status": "answered", "question": req.question, "reading": reading_json, "client": client,
                   "line": line, "line_label": LINES[line], "answer": payloads.answer(answer), "chain": chain,
                   "method": payloads.method(now),
                   "worksheet": {"before": payloads.worksheet(start, sheet_lines), "after": payloads.worksheet(now, sheet_lines)},
                   "amended": payloads.amended(ret, now),
                   "parity": payloads.parity_view(parity), "authorities": payloads.authorities(authorities),
                   "moves": data["moves"], "book": payloads.book_rows(state, {}, CASES)})
    stream.finished("answered")


def _explain_session(stream: Stream, req: RunRequest, models: Models, session: Session, reading: questions.Reading,
                     reading_json: dict[str, Any]) -> None:
    """A question about the session as a whole, answered from the approved changes and the current results."""
    state = session.state
    for node, why in (("documents", "a question reads no document"), ("validate", "a question changes nothing, so there is nothing to validate")):
        stream.skip(node, why)
    stream.start("engine_a", "looking up the session's results")
    latest: dict[str, tuple[ChangeSet, Any]] = {}
    for run in session.runs:
        for item in run.report.reached if run.report else []:
            latest[item.client_id] = (run.change, item)
    waiting = {cid: result.needs for cid, result in state.results.items() if result.needs}
    stream.finish("engine_a", "done", f"{len(state.results)} returns in the session; {len(session.runs)} approved "
                  f"{'change' if len(session.runs) == 1 else 'changes'}; {len(waiting)} {'return waits' if len(waiting) == 1 else 'returns wait'} for a document",
                  provider="Engine A results from the replayed session", output={"waiting": {k: list(v) for k, v in waiting.items()}})
    stream.skip("engine_b", "a question about the session needs no new cross-check")
    stream.skip("law", "a question changes no rule or fact")
    stream.start("route")
    counts: dict[str, int] = {}
    for _, item in latest.values():
        counts[item.route] = counts.get(item.route, 0) + 1
    stream.finish("route", "done", ", ".join(f"{ROUTES[k].lower()} {v}" for k, v in counts.items()) or "no approved change has routed a return",
                  provider=PROVIDER["route"], output=counts)
    stream.skip("approve", "a question changes nothing, so there is nothing to approve")
    stream.skip("notes", "a question writes no notes")
    data = {"latest": {cid: {"change": c.id, "route": item.route, "vs_filed": payloads.vs_filed_of(item)}
                       for cid, (c, item) in latest.items()}, "waiting": {k: list(v) for k, v in waiting.items()}}
    answer = _answer(stream, lambda: explain.session_answer(reading.topic, latest, waiting, len(session.runs)), req.question, data)
    stream.skip("audit", "a question about the session changes nothing, so nothing is recorded")
    stream.result({"kind": "explain", "status": "answered", "question": req.question, "reading": reading_json, "client": "",
                   "topic": reading.topic, "answer": payloads.answer(answer), "book": payloads.book_rows(state, {}, CASES)})
    stream.finished("answered")


# The decision run --------------------------------------------------------------------------------------


def decision(req: DecisionRequest, models: Models, on_done: Callable[[], None] | None = None) -> Stream:
    stream = Stream(req.run_id or new_run_id())
    return _spawn(stream, req.question or req.change.title, lambda: _decide(stream, req, models), on_done)


def _decide(stream: Stream, req: DecisionRequest, models: Models) -> None:
    stream.put({"type": "run_started", "mode": "decision", "decision": req.decision, "question": req.question, "at": _now(),
                "history": [item.change.id for item in req.history], "engine": __version__})
    stream.start("approve", "recomputing the preview to check it matches what you saw")
    tail = ("notes", "explain", "audit")
    session = _replay_or_fail(stream, req.history, "approve")
    if session is None:
        return

    def refuse(reason: str, status: str = "stale") -> None:
        stream.finish("approve", "failed", reason, provider="reviewer", error=reason)
        stream.skip_rest(tail, "not run: nothing was approved")
        stream.result({"kind": "change", "status": status, "message": reason, "change": req.change.model_dump(mode="json")})
        stream.finished(status)

    if req.hypothetical or any(is_hypothetical(edit.cite) for edit in req.change.rule_edits):
        refuse("a what-if is a preview only; it cannot be approved or turned down", "rejected")
        return
    if req.decision == "approve" and len(req.history) >= MAX_HISTORY:
        refuse(f"this session already holds {MAX_HISTORY} approved changes, the most it replays; reset the session to approve more", "history_full")
        return
    problems = check_change(req.change)
    if problems:
        refuse("; ".join(problems), "rejected")
        return
    started = perf_counter()
    preview = run_change(session.state, req.change, materiality=req.materiality, parity_mode=PARITY_MODE, law_mode="cache", docling_live=DOCLING_LIVE)
    check_ms = (perf_counter() - started) * 1000 + session.ms
    if preview.pending is None or preview.state is None:
        refuse("the change no longer passes its checks; preview it again")
        return
    digest = signing.preview_digest(preview, req.materiality)
    if not signing.verify(req.preview_token, "preview", digest=digest, tip=tip(req.history), hypothetical=req.hypothetical):
        refuse("this is not the preview the server showed for this session (the session or the change set differs); preview the change again")
        return
    preview_events = len(preview.state.audit)
    name = req.approver.strip()
    check = f"preview recomputed in {check_ms:.1f} ms and matched what you saw"
    base = {"kind": "change", "question": req.question, "change": req.change.model_dump(mode="json"), "materiality": str(req.materiality),
            "hypothetical": req.hypothetical, "change_view": payloads.change_view(preview.change, session.state.pack, session.state.book),
            "pack_before": payloads.pack_view(session.state.pack)}
    report = preview.report
    assert report is not None and preview.validation is not None and preview.pending is not None

    if req.decision == "reject":
        state = record_rejection(preview, name or "reviewer")
        stream.finish("approve", "done", f"turned down by {name or 'reviewer'}; nothing applied ({check})", provider="reviewer",
                      decision="turned_down", output={"approver": name or "reviewer", "decision": "turned down"})
        stream.skip("notes", "turned down: no notes are written")
        answer = _answer(stream, lambda: explain.change_answer(preview.change, report, session.state.pack,
                                                               payloads.facts_before(preview.change, session.state.book),
                                                               decision="turned down", approver=name or "reviewer", hypothetical=req.hypothetical),
                         req.question or req.change.title,
                         {"kpis": payloads.kpis(report, preview.pending.parity), "cli": run_json(preview), "change": payloads.change_view(preview.change, session.state.pack, session.state.book)})
        events = payloads.audit(state.audit[preview_events:])
        _audit_node(stream, events)
        stream.result({**base, "status": "turned_down", "answer": payloads.answer(answer), "audit": events,
                       "book": payloads.book_rows(session.state, {}, CASES), "cli": run_json(preview)})
        stream.finished("turned_down")
        return

    def on_start(lane: str) -> None:
        stream.check()
        stream.start(lane, "writing the notes")

    def on_lane(lane: LaneResult) -> None:
        if lane.lane == "approve":
            if lane.state == "failed":
                stream.finish("approve", "failed", lane.summary, provider="reviewer", error=lane.summary)
            else:
                stream.finish("approve", "done", f"{lane.summary}; {check}", provider="reviewer (typed name, not verified)",
                              decision="approved", output={"approver": name})
        elif lane.lane == "notes":
            pending_notes.append(lane)  # reported below, with the LLM calls the meter saw

    pending_notes: list[LaneResult] = []
    provider = models.provider if NOTES_WORDING else None
    applied, calls = _metered(lambda: approve_run(preview, name, notes_provider=provider, on_lane=on_lane, on_start=on_start))
    if not applied.applied:
        stream.skip_rest(tail[:2], "not run: approval needs the approver's name")
        events = payloads.audit(applied.state.audit[preview_events:]) if applied.state else []
        _audit_node(stream, events)
        stream.result({**base, "status": "rejected", "errors": payloads.errors(applied.validation), "audit": events})
        stream.finished("rejected")
        return
    lane = pending_notes[0]
    reworded = [c for c in calls if c.get("provider")]
    fallback = None
    if not provider:
        fallback = f"templates: {models.why_not}" if NOTES_WORDING else "templates: wording is turned off on this server"
    elif any(source.startswith("template (") for source in applied.note_sources.values()):
        fallback = "some notes kept their template: " + "; ".join(sorted({s for s in applied.note_sources.values() if s.startswith("template (")}))[:300]
    reused = bool(reworded) and all(call.get("outcome") == "reused reply" for call in reworded)
    stream.finish("notes", "done", lane.summary, duration_ms=lane.ms,
                  source="fallback" if fallback and not reworded else "cached" if reused else "live",
                  provider=llm.PROVIDERS.get(provider or "", "") + ", figures checked" if reworded else "templates (figures inserted by code)",
                  output={cid: {"text": text, "source": applied.note_sources.get(cid, "")} for cid, text in applied.notes.items()},
                  request=_request(calls), fallback=fallback)

    assert applied.state is not None
    answer = _answer(stream, lambda: explain.change_answer(applied.change, report, session.state.pack,
                                                           payloads.facts_before(applied.change, session.state.book),
                                                           decision="approved", approver=name, hypothetical=req.hypothetical),
                     req.question or req.change.title,
                     {"kpis": payloads.kpis(report, applied.state.parity), "cli": run_json(applied), "change": payloads.change_view(applied.change, session.state.pack, session.state.book)})
    events = payloads.audit(applied.state.audit[preview_events:])
    _audit_node(stream, events)
    sources = Sources(dict(session.sources.rules), dict(session.sources.facts))
    sources.record(applied)
    receipt = receipt_for(tip(req.history), req.change, name, req.materiality, digest)
    notes = [{"client": cid, "kind": "reviewer", "route": item.route, "text": applied.notes[cid], "source": applied.note_sources.get(cid, "template")}
             for item in report.reached for cid in [item.client_id] if cid in applied.notes]
    notes += [{"client": cid, "kind": kind, "route": item.route, "text": text, "source": "template (letters to clients are never reworded)"}
              for item in report.reached for cid in [item.client_id] for kind, text in applied.letters.get(cid, [])]
    stream.result({
        **base,
        "status": "applied",
        "story": payloads.story(report, applied.change, preview.validation.touched, applied=True),
        "history_item": Approved(change=req.change, approver=name, materiality=req.materiality, receipt=receipt).model_dump(mode="json"),
        "pack_after": payloads.pack_view(applied.state.pack),
        "facts_after": payloads.facts_versions(applied.state.book),
        "kpis": payloads.kpis(report, applied.state.parity),
        "impact": [payloads.impact_item(item, applied.change, book=applied.state.book, pack=applied.state.pack, parity=applied.state.parity,
                                        sources=sources, touched=preview.validation.touched, materiality=req.materiality) for item in report.reached],
        "not_reached": payloads.not_reached_rows(report, session.state.results, preview.validation.touched),
        "authorities": payloads.authorities(preview.authorities),
        "notes": notes,
        "answer": payloads.answer(answer),
        "audit": events,
        "book": payloads.book_rows(applied.state, {item.client_id: item.route for item in report.reached}, CASES),
        "cli": run_json(applied),
    })
    stream.finished("applied")


def drain(stream: Stream) -> list[dict[str, Any]]:
    """Every event of a run, for tests and the command line."""
    return [json.loads(line) for line in stream.lines()]
