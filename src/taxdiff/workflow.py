"""Run a change set through the lanes and keep the book's state and audit log.

Order: intake or documents, then validation, then Engine A, which recomputes the book and lists the returns
the change reaches. Engine B and the law lane then run side by side, and routing waits for Engine B. Up to
this point a run is a preview: it stops at the approval lane with the routes and amounts the change would
produce, and nothing is applied. approve_run applies that preview's result as it stands, then writes the
notes.

Every run ends in one audit event (previewed, failed validation, stopped, not recognized or error), and a
decision on a preview adds an applied, turned down or refused event. An optional lane that fails (Engine B, law) is reported as failed and the run goes on without it; a
failure anywhere else stops the change and the last valid version stays in effect. Each lane result is
reported through an optional callback, on the caller's thread, as soon as the lane finishes, so a screen can
show the run while it happens; a second optional callback reports each lane as it starts.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from . import __version__
from .changes import ChangeSet, Validation, validate_change
from .engine import ComputedReturn, compute
from .facts import Book, ClientReturn
from .impact import ImpactReport, ReturnImpact, analyze, compute_book, route_all
from .lanes import documents, engine_b, law, notes
from .lanes.notes import money, signed
from .paths import ROOT
from .provenance import Tracker, Unknown, Value, total
from .provisions import OVERTIME
from .router import ROUTES
from .rules import FieldError, RulePack, edit_pack, is_hypothetical

ParityMode = Literal["cache", "live", "api", "off"]
LawMode = Literal["cache", "live"]
ENGINE = f"taxdiff {__version__}"
SOURCES = {"cache": "cached results", "live": "live run", "api": "live through the PolicyEngine API", "off": "off", "failed": "failed"}


def _engine_name(lane: engine_b.LaneOutput) -> str:
    """How the parity engine is named in a summary: the API, or the policyengine-us release of the results."""
    if lane.source == "api":
        return ("PolicyEngine-US, an answer the PolicyEngine API gave earlier on this server, reused" if lane.reused
                else "PolicyEngine-US, live through the PolicyEngine API")
    release = f" {lane.version}" if lane.version else ""
    return f"PolicyEngine-US{release}, {SOURCES.get(lane.source, lane.source)}"


# At most this many reviewer notes are reworded by a model per approval; document requests never are.
NOTES_REWORD_MAX = int(os.environ.get("TAXDIFF_NOTES_REWORD_MAX", "8"))
_REWORD_ORDER = ("amendment", "reviewer", "update", "below_materiality", "no_change", "document")


@dataclass(frozen=True)
class LaneResult:
    lane: str
    state: Literal["done", "failed", "skipped", "waiting"]
    ms: float
    summary: str
    # The lane's own output objects (the impact report, Engine B's output, the authorities...), for callers
    # that show more than the summary. Not part of equality or the printed form.
    detail: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class AuditEvent:
    """One entry per outcome: what happened to the change, who approved it, and what it ran against."""

    at: str
    change_id: str
    title: str
    kind: str
    author: str
    outcome: Literal["applied", "previewed", "turned down", "failed validation", "stopped", "not recognized", "refused", "error"]
    detail: str
    pack_version: int
    approver: str = ""
    pack_sha: str = ""  # content hash, so saved outputs from two sessions can tell two different "v2" packs apart
    facts: str = ""  # facts versions of the clients a fact change touches
    materiality: str = ""
    parity: str = ""  # how Engine B ran and what it found
    engine: str = ENGINE


@dataclass(frozen=True)
class State:
    pack: RulePack
    book: Book
    results: dict[str, ComputedReturn]
    as_filed: dict[str, Mapping[str, Value]]
    parity: dict[str, engine_b.Parity]
    parity_note: str = ""
    audit: tuple[AuditEvent, ...] = ()
    drift: dict[str, tuple[str, ...]] = field(default_factory=dict)  # filed returns that no longer recompute to their filed amounts


def pack_digest(pack: RulePack) -> str:
    """The first 12 hex digits of a SHA-256 over the pack's parameters and citations, without its version."""
    content = {key: value for key, value in pack.raw.items() if key != "version"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _unmapped_reads(results: dict[str, ComputedReturn], pack: RulePack) -> dict[str, str]:
    _, unmapped = engine_b.reform_for(pack, engine_b.baseline_pack())
    marked = {}
    for client_id, result in results.items():
        hit = [key for key in unmapped if f"rule:{key}" in result.all_reads]
        if hit:
            marked[client_id] = f"{', '.join(hit)} not passed to PolicyEngine-US"
    return marked


def filed_amounts(
    book: Book, results: dict[str, ComputedReturn]
) -> tuple[dict[str, Mapping[str, Value]], dict[str, tuple[str, ...]]]:
    """The amounts each filed return was filed with, and the filed returns that no longer recompute to them.

    Every filed return in a book records the line values it was filed with. One that recomputes to other
    values under the starting pack and the current engine is listed as drift, with the lines that differ:
    an engine release, or a starting pack other than the one it was filed under, which no change set
    describes.
    """
    as_filed: dict[str, Mapping[str, Value]] = {}
    drift: dict[str, tuple[str, ...]] = {}
    for ret in book.returns:
        if ret.status != "filed" or ret.as_filed is None:
            continue
        now = results[ret.client_id].values
        as_filed[ret.client_id] = ret.as_filed
        moved = tuple(line for line, value in ret.as_filed.items() if now.get(line) != value)
        if moved:
            drift[ret.client_id] = moved
    return as_filed, drift


def _parity(book: Book, pack: RulePack, mode: ParityMode) -> engine_b.LaneOutput:
    """Engine B's output; a lane that raises leaves every return not checked."""
    try:
        return engine_b.run_lane(book, pack, mode)
    except Exception as exc:  # an optional lane: report it and go on without it
        return engine_b.unavailable(book, f"PolicyEngine-US could not run ({type(exc).__name__}: {exc})")


def initial_state(pack: RulePack, book: Book, parity_mode: ParityMode = "cache") -> State:
    results = compute_book(book, pack)
    lane = _parity(book, pack, parity_mode)
    parity = engine_b.parity_for(results, lane, _unmapped_reads(results, pack))
    as_filed, drift = filed_amounts(book, results)
    return State(pack, book, results, as_filed, parity, lane.note, drift=drift)


@dataclass
class Run:
    change: ChangeSet
    lanes: list[LaneResult] = field(default_factory=list)
    validation: Validation | None = None
    report: ImpactReport | None = None
    notes: dict[str, str] = field(default_factory=dict)
    note_sources: dict[str, str] = field(default_factory=dict)
    authorities: list[law.Authority] = field(default_factory=list)
    state: State | None = None  # the session state after this run, its audit events included
    pending: State | None = None  # for a preview: the state approval applies
    applied: bool = False
    materiality: Decimal = Decimal(50)
    parity: str = ""  # Engine B's release, mode and counts, for the audit
    facts: str = ""  # facts versions a fact change moves, for the audit
    detail: str = ""  # reach and route counts, for the audit
    citations: dict[str, dict[str, Any]] = field(default_factory=dict)  # rule path -> what looking up its cited authority found
    letters: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # client -> (kind, text) letters to the client

    def lane(self, name: str) -> LaneResult | None:
        return next((item for item in self.lanes if item.lane == name), None)


def _guarded(fn: Callable[..., Any], *args: Any) -> tuple[Any, float, str]:
    """Run one lane and time it. Returns (value, ms, error); a lane that raises gives its error as text."""
    started = perf_counter()
    try:
        value, error = fn(*args), ""
    except Exception as exc:  # reported by the caller; a lane never raises past this point
        value, error = None, f"{type(exc).__name__}: {exc}"
    return value, (perf_counter() - started) * 1000, error


def _audit(
    state: State,
    change: ChangeSet,
    outcome: str,
    detail: str,
    *,
    pack: RulePack,
    approver: str = "",
    facts: str = "",
    materiality: Decimal | None = None,
    parity: str = "",
) -> State:
    event = AuditEvent(
        at=datetime.now().astimezone().isoformat(timespec="seconds"),  # with its offset, so a browser shows its own clock
        change_id=change.id,
        title=change.title,
        kind=change.kind,
        author=change.author,
        outcome=outcome,  # type: ignore[arg-type]
        detail=detail,
        pack_version=pack.version,
        approver=approver,
        pack_sha=pack_digest(pack),
        facts=facts,
        materiality="" if materiality is None else f"${materiality}",
        parity=parity,
    )
    return replace(state, audit=state.audit + (event,))


def record_intake_refusal(state: State, change_id: str, text: str, source: str, reason: str) -> State:
    """Audit an instruction the intake lane could not read or refused. Nothing else runs for it."""
    lines = text.strip().splitlines()
    event = AuditEvent(
        at=datetime.now().astimezone().isoformat(timespec="seconds"),  # with its offset, so a browser shows its own clock
        change_id=change_id,
        title=lines[0][:120] if lines else "",
        kind="instruction",
        author=source,
        outcome="not recognized",
        detail=f"intake: {reason}",
        pack_version=state.pack.version,
        pack_sha=pack_digest(state.pack),
    )
    return replace(state, audit=state.audit + (event,))


def record_rejection(preview: Run, approver: str, reason: str = "") -> State:
    """Audit a preview the reviewer turned down at the approval step. Nothing is applied."""
    if preview.pending is None or preview.state is None:
        raise ValueError("only a preview that passed its checks can be turned down")
    name = approver.strip() or "reviewer"
    detail = f"turned down at approval by {name}" + (f": {reason}" if reason else "") + f"; preview: {preview.detail}"
    return _audit(preview.state, preview.change, "turned down", detail, pack=preview.state.pack, approver=name,
                  facts=preview.facts, materiality=preview.materiality, parity=preview.parity)


def _lane_result(name: str, value: Any, ms: float, error: str) -> LaneResult:
    """The board entry for Engine A, Engine B or Law."""
    if name == "engine_a":
        if value is None:
            return LaneResult(name, "failed", ms, f"stopped: {error}")
        return LaneResult(
            name,
            "failed" if value.unexpected else "done",
            ms,
            f"reached {len(value.reached)} of {len(value.results)} returns by recorded reads; "
            f"recomputed the book in {value.ms:.1f} ms; {len(value.unexpected)} unexpected changes",
            detail={"report": value},
        )
    if name == "engine_b":
        if value is None:
            return LaneResult(name, "failed", ms, f"PolicyEngine-US could not run ({error}); no return is checked")
        note = f"; {value.note}" if value.note and value.source != "api" else ""
        summary = (f"{_engine_name(value)}: {len(value.results)} returns checked, {len(value.skipped)} not checked; "
                   f"it checks the arithmetic and takes the qualified overtime from Engine A{note}")
        if value.fallback:
            summary += f" ({value.fallback})"
        return LaneResult(name, "done" if value.results else "skipped", ms, summary, detail={"output": value})
    if value is None:
        return LaneResult(name, "failed", ms, f"no authorities attached ({error})")
    excerpts = [a for a in value if a.excerpt]
    cited = [a for a in value if not a.key.startswith("fr-")]
    related = [a for a in value if a.key.startswith("fr-") and not a.key.startswith(("fr-none-", "fr-failed-"))]
    if value:
        live = [a for a in excerpts if a.fetched]
        missed = next((a.fallback for a in cited if a.fallback), "")
        n = len(cited)
        if live:
            summary = f"{n} {'authority' if n == 1 else 'authorities'} attached; FLSA excerpts fetched live from the eCFR API (eCFR up to date as of {live[0].as_of})"
        else:
            summary = f"{n} {'authority' if n == 1 else 'authorities'} attached" + (f"; FLSA excerpts copied from eCFR as of {excerpts[0].as_of}" if excerpts else "")
            if missed:
                summary += f" (live fetch failed: {missed})"
        found = [a for a in cited if a.checked.startswith("found")]
        if any(a.checked for a in cited):
            summary += f"; {len(found)} of {sum(1 for a in cited if a.checked)} cited sources answered at their address"
        if any(a.key.startswith("fr-") for a in value):
            summary += (f"; Federal Register: {len(related)} related Treasury or IRS {'document' if len(related) == 1 else 'documents'}"
                        + (f", newest {related[0].title.split(':')[0].removeprefix('Federal Register, ')}" if related else ""))
    else:
        summary = "fact change: authorities follow the changed lines"
    return LaneResult(name, "done", ms, summary, detail={"authorities": value})


def _parity_text(lane: engine_b.LaneOutput, parity: dict[str, engine_b.Parity]) -> str:
    counts = {state: sum(p.state == state for p in parity.values()) for state in ("agree", "differs", "not_checked")}
    return (
        f"{_engine_name(lane)}: "
        f"{counts['agree']} agree, {counts['differs']} differ, {counts['not_checked']} not checked"
    )


# Facts that are evidence of the FLSA overtime premium: a statement or corrected facts that set one of them
# are checked against the return as filed.
_PREMIUM_EVIDENCE = ("ot_premium_reported", "ot_premium_paid", "ot_flsa_hours", "ot_regular_rate")


def _conflict(item: ReturnImpact, ret: ClientReturn | None, pack: RulePack, touched: set[tuple[str | None, str]], kind: str) -> tuple[str, Decimal | None]:
    """A new statement or corrected facts that support less qualified overtime, under IRS Notice 2025-69's
    methods, than the return deducts (as filed, or as prepared for a return in progress), in words; or "". Checked
    whatever the active rules compute, since a statement can arrive before the rules are corrected. The deduction
    on a return can never exceed the qualified overtime it is figured from. Returns (text, the priced change or
    None)."""
    against = item.filed_values if item.status == "filed" else item.before.values
    if ret is None or not against or "sch1a_overtime" not in against:
        return "", None
    edited = {path for client, path in touched if client == item.client_id}
    people = [i for i, _ in enumerate(ret.people) if any(f"fact:people[{i}].{name}" in edited for name in _PREMIUM_EVIDENCE)]
    if not people:
        return "", None
    tr = Tracker(pack)
    supported = total(*(OVERTIME["flsa_premium"].compute(tr, i, ret.people[i]) for i in people))
    deducted = against["sch1a_overtime"]
    if isinstance(supported, Unknown) or isinstance(deducted, Unknown) or deducted <= supported:
        return "", None
    source = "statement supports" if kind == "document" else "corrected facts support"
    where = "the return as filed deducted" if item.status == "filed" else "the prepared return deducts"
    text = f"The {source} {money(supported)} of qualified overtime under IRS Notice 2025-69; {where} {money(deducted)}."
    delta = _priced(ret, pack, against)
    if delta is not None:
        whose = "the return as filed" if item.status == "filed" else "the prepared return"
        text += f" Under the Notice's rule its tax would be {signed(delta)} against {whose}."
    return text, delta


def _priced(ret: ClientReturn, pack: RulePack, against: Mapping[str, Value]) -> Decimal | None:
    """While the rules still count all overtime pay, the change in tax once the Notice's rule applies, against the
    return as filed or as prepared; None once the rules already follow the Notice."""
    if pack.value("overtime.definition") == "flsa_premium":
        return None
    notice, _ = edit_pack(pack, [{"path": "overtime.definition", "to": "flsa_premium", "cite": "IRS Notice 2025-69"}])
    tax, base = (compute(ret, notice).values.get("tax") if notice is not None else None), against.get("tax")
    if not isinstance(tax, Decimal) or not isinstance(base, Decimal):
        return None
    return tax - base


def _validated(change: ChangeSet, validation: Validation) -> str:
    if change.kind == "rule":
        assert validation.pack is not None
        n = len(change.rule_edits)
        return f"valid: {n} rule {'edit' if n == 1 else 'edits'}; next rule pack v{validation.pack.version}"
    clients = sorted({client for client, _ in validation.touched if client})
    n = len(change.fact_edits)
    return f"valid: {n} fact {'edit' if n == 1 else 'edits'} for {', '.join(clients)}"


def run_change(
    state: State,
    change: ChangeSet,
    *,
    approve: bool = False,
    materiality: Decimal = Decimal(50),
    parity_mode: ParityMode = "cache",
    intake: LaneResult | None = None,
    notes_provider: str | None = None,
    docling_live: bool = False,
    on_lane: Callable[[LaneResult], None] | None = None,
    approver: str = "reviewer",
    on_start: Callable[[str], None] | None = None,
    law_mode: LawMode = "cache",
) -> Run:
    """Run one change set as a preview: every lane up to approval, with nothing applied.

    The preview carries the routes and amounts in run.report and, in run.pending, the state approval would
    apply. With approve=True the preview is approved at once by `approver`, as the command line's --approve
    does. on_start(lane) is called as each lane starts; law_mode="live" lets the law lane fetch the FLSA
    excerpts from the eCFR API, falling back to the saved excerpts.
    """
    run = Run(change=change, state=state, materiality=materiality)

    def emit(lane: LaneResult) -> None:
        run.lanes.append(lane)
        if on_lane is not None:
            on_lane(lane)

    def begin(name: str) -> None:
        if on_start is not None:
            on_start(name)

    try:
        _preview(run, state, change, emit, begin, materiality=materiality, parity_mode=parity_mode, intake=intake,
                 docling_live=docling_live, law_mode=law_mode)
    except Exception as exc:  # never leave a change neither applied nor rejected
        reason = f"stopped by an unexpected error ({type(exc).__name__}: {exc}); nothing was applied"
        run.validation = Validation(errors=[FieldError("run", reason)])
        run.pending = None
        run.state = _audit(state, change, "error", reason, pack=state.pack, materiality=materiality)
    if approve and run.pending is not None:
        return approve_run(run, approver, notes_provider=notes_provider, on_lane=on_lane, on_start=on_start)
    return run


def _stop(run: Run, state: State, change: ChangeSet, emit: Callable[[LaneResult], None], outcome: str, field_name: str, reason: str) -> None:
    """Stop a change after Engine A: the lanes after it are skipped and the last valid version stays."""
    run.validation = Validation(errors=[FieldError(field_name, reason)])
    for name in ("engine_b", "law", "route"):
        emit(LaneResult(name, "skipped", 0.0, "not run: the change was stopped"))
    run.state = _audit(state, change, outcome, reason, pack=state.pack, materiality=run.materiality)


def _preview(
    run: Run,
    state: State,
    change: ChangeSet,
    emit: Callable[[LaneResult], None],
    begin: Callable[[str], None],
    *,
    materiality: Decimal,
    parity_mode: ParityMode,
    intake: LaneResult | None,
    docling_live: bool,
    law_mode: LawMode,
) -> None:
    emit(intake or LaneResult("intake", "skipped", 0.0, "structured change set, no text to read"))

    if change.kind == "document":
        begin("documents")
        started = perf_counter()
        path = Path(change.document or "")
        path = path if path.is_absolute() else ROOT / path
        try:
            text, method = documents.read_text(path, use_cache=not docling_live)
            statement = documents.read_statement(text, method, change.client_id or "", state.book)
        except (OSError, ValueError) as exc:
            statement = None
            errors = [FieldError("document", str(exc))]
        else:
            errors = statement.errors
        ms = (perf_counter() - started) * 1000
        if errors:
            detail = "; ".join(str(e) for e in errors)
            emit(LaneResult("documents", "failed", ms, detail, detail={"statement": statement, "errors": errors}))
            run.validation = Validation(errors=errors)
            run.state = _audit(state, change, "failed validation", detail, pack=state.pack, materiality=materiality)
            return
        assert statement is not None
        change = change.model_copy(update={"fact_edits": statement.edits})
        run.change = change
        fields = ", ".join(f"{k} {v}" for k, v in statement.fields.items() if v is not None and k != "employee")
        emit(LaneResult("documents", "done", ms, f"{method}: read {statement.read_count} of {len(statement.fields)} fields ({fields})",
                        detail={"statement": statement}))
    else:
        emit(LaneResult("documents", "skipped", 0.0, "no document in this change"))

    begin("validate")
    started = perf_counter()
    validation = validate_change(change, state.pack, state.book)
    if validation.ok and law_mode == "live" and change.kind == "rule":
        # Each cited authority with a predictable address is looked up; one that is not there stops the change.
        edits = [edit for edit in change.rule_edits if not is_hypothetical(edit.cite)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            run.citations = dict(zip((edit.path for edit in edits), pool.map(lambda edit: law.check_citation(edit.cite), edits), strict=True))
        validation.errors += [FieldError(f"{path}.cite", f"{found['detail']}; cite an authority that exists, or write Hypothetical for a what-if")
                              for path, found in run.citations.items() if found["state"] == "not_found"]
    ms = (perf_counter() - started) * 1000
    run.validation = validation
    if not validation.ok:
        detail = "; ".join(str(error) for error in validation.errors)
        emit(LaneResult("validate", "failed", ms, detail, detail={"validation": validation, "change": change}))
        run.state = _audit(state, change, "failed validation", detail, pack=state.pack, materiality=materiality)
        return
    emit(LaneResult("validate", "done", ms, _validated(change, validation), detail={"validation": validation, "change": change}))
    pack, book = validation.pack, validation.book
    assert pack is not None and book is not None

    # Engine A runs first: it can stop the change, and then nothing else needs to run.
    begin("engine_a")
    report, ms, error = _guarded(analyze, validation.touched, state.results, state.as_filed, book, pack)
    if report is not None:
        run.report = report
    emit(_lane_result("engine_a", report, ms, error))
    if report is None:
        _stop(run, state, change, emit, "error", "engine_a", f"Engine A stopped ({error}); the change is not applied")
        return
    if report.unexpected:
        # The full recompute caught a return the recorded reads missed: fail closed and keep the last valid version.
        reason = (
            f"full recompute: {len(report.unexpected)} returns outside the reached list moved "
            f"({', '.join(report.unexpected)}); the change is not applied"
        )
        _stop(run, state, change, emit, "stopped", "engine_a", reason)
        return

    touched_rules = [path.removeprefix("rule:") for _, path in validation.touched if path.startswith("rule:")]
    calls = {"engine_b": (engine_b.run_lane, book, pack, parity_mode), "law": (law.for_rules, touched_rules, law_mode == "live")}
    found: dict[str, Any] = {}
    errors_by_lane: dict[str, str] = {}
    for name in calls:
        begin(name)
    # Each worker runs in a copy of the caller's context, so per-request context (an LLM meter) follows it.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(contextvars.copy_context().run, _guarded, *call): name for name, call in calls.items()}
        for future in as_completed(futures):
            name = futures[future]
            value, ms, error = future.result()
            found[name], errors_by_lane[name] = value, error
            emit(_lane_result(name, value, ms, error))
    lane_b: engine_b.LaneOutput = found["engine_b"] or engine_b.unavailable(book, f"PolicyEngine-US could not run ({errors_by_lane['engine_b']})")
    run.authorities = found["law"] or []

    begin("route")
    started = perf_counter()
    if change.kind != "rule":
        priced = {item.client_id: _conflict(item, book.get(item.client_id), pack, validation.touched, change.kind) for item in report.reached}
        report.reached = [replace(item, conflict=priced[item.client_id][0], conflict_delta=priced[item.client_id][1])
                          for item in report.reached]
    parity = engine_b.parity_for(report.results, lane_b, _unmapped_reads(report.results, pack))
    route_all(report, materiality, {client_id for client_id, result in parity.items() if result.state == "differs"})
    counts = report.counts()
    summary = ", ".join(f"{ROUTES[k].lower()} {v}" for k, v in sorted(counts.items(), key=lambda kv: list(ROUTES).index(kv[0])))
    if report.reached:
        checked = sum(parity[item.client_id].state != "not_checked" for item in report.reached)
        summary += f"; PolicyEngine-US checked {checked} of {len(report.reached)}"
        if checked < len(report.reached):
            summary += ", the rest route by status alone"
    emit(LaneResult("route", "done", (perf_counter() - started) * 1000, summary or "nothing to route",
                    detail={"report": report, "parity": parity, "materiality": materiality, "lane_b": lane_b}))

    clients = sorted({client for client, _ in validation.touched if client})
    run.facts = ", ".join(f"{cid} v{state.book.get(cid).facts_version} to v{book.get(cid).facts_version}" for cid in clients)  # type: ignore[union-attr]
    run.detail = (f"reached {len(report.reached)}, not reached {len(report.not_reached)}, unexpected {len(report.unexpected)}; "
                  + ", ".join(f"{ROUTES[k].lower()} {v}" for k, v in sorted(counts.items(), key=lambda kv: list(ROUTES).index(kv[0]))))
    run.parity = _parity_text(lane_b, parity)
    emit(LaneResult("approve", "waiting", 0.0, "waiting for a reviewer; nothing is applied until someone approves", detail={"report": report}))
    run.state = _audit(state, change, "previewed", run.detail, pack=state.pack, facts=run.facts, materiality=materiality, parity=run.parity)
    next_state = State(pack, book, report.results, state.as_filed, parity, lane_b.note, run.state.audit, state.drift)
    run.pending = next_state


def approve_run(
    preview: Run,
    approver: str,
    *,
    notes_provider: str | None = None,
    on_lane: Callable[[LaneResult], None] | None = None,
    on_start: Callable[[str], None] | None = None,
) -> Run:
    """Apply a preview's result as it stands, write the notes, and record who approved it.

    Nothing is recomputed: the routes and amounts applied are the ones the preview showed, against the state
    the preview ran on, so callers approve only the latest preview. A blank approver is refused with a
    refused audit event and nothing is applied.
    """
    if preview.pending is None or preview.report is None or preview.applied or preview.state is None:
        raise ValueError("only a preview that passed its checks can be approved")
    run = replace(preview, lanes=[lane for lane in preview.lanes if lane.lane not in ("approve", "notes")], notes={}, note_sources={}, pending=None)

    def emit(lane: LaneResult) -> None:
        run.lanes.append(lane)
        if on_lane is not None:
            on_lane(lane)

    name = approver.strip()
    if any(is_hypothetical(edit.cite) for edit in run.change.rule_edits):
        reason = "a what-if (its citation is a hypothesis, not an authority) is previewed only and never applied"
        emit(LaneResult("approve", "failed", 0.0, reason))
        run.validation = Validation(errors=[FieldError("cite", reason)])
        run.state = _audit(preview.state, run.change, "refused", reason, pack=preview.state.pack, materiality=preview.materiality)
        return run
    if not name:
        reason = "approval needs the approver's name"
        emit(LaneResult("approve", "failed", 0.0, reason))
        run.validation = Validation(errors=[FieldError("approver", reason)])
        run.state = _audit(preview.state, run.change, "refused", reason, pack=preview.state.pack, materiality=preview.materiality)
        return run
    emit(LaneResult("approve", "done", 0.0, f"approved by {name}"))

    if on_start is not None:
        on_start("notes")
    started = perf_counter()
    report, applied = preview.report, preview.pending
    cause = f"{run.change.id} {run.change.title}"
    for item in report.reached:
        # The internal note and the letters to the client are kept apart: only internal notes may be reworded.
        run.notes[item.client_id] = notes.reviewer_note(item, cause)
        found = notes.letters(item, applied.book.tax_year, preview.materiality)
        if found:
            run.letters[item.client_id] = found
    run.note_sources = {client_id: "template" for client_id in run.notes}
    count = sum(len(found) for found in run.letters.values())
    notes_summary = f"{len(run.notes)} internal notes and {count} {'letter' if count == 1 else 'letters'} from templates; figures inserted by code"
    internal = {item.client_id: ROUTES[item.route] for item in report.reached}
    if notes_provider and len(internal) > NOTES_REWORD_MAX:
        # Reword the notes that ask for action first; the rest keep their templates, so one approval makes a
        # bounded number of model calls.
        ranked = sorted(internal, key=lambda cid: _REWORD_ORDER.index(next(i.route for i in report.reached if i.client_id == cid)))
        for client_id in ranked[NOTES_REWORD_MAX:]:
            run.note_sources[client_id] = f"template (at most {NOTES_REWORD_MAX} notes are reworded per approval)"
        internal = {cid: internal[cid] for cid in ranked[:NOTES_REWORD_MAX]}
    if notes_provider and internal:
        # Each rewording runs in its own copy of the caller's context, taken here on the caller's thread, so
        # per-request context (an LLM meter) follows it into the pool.
        contexts = {cid: contextvars.copy_context() for cid in internal}
        with ThreadPoolExecutor(max_workers=4) as pool:
            polished = dict(
                zip(
                    internal,
                    pool.map(
                        lambda cid: contexts[cid].run(notes.polish, run.notes[cid], cid, notes_provider, route=internal[cid]),
                        internal,
                    ),
                    strict=True,
                )
            )
        for client_id, (text, source) in polished.items():
            run.notes[client_id], run.note_sources[client_id] = text, source
        reworded = sum(source.startswith("LLM") for source in run.note_sources.values())
        notes_summary = (
            f"{len(run.notes)} {'note' if len(run.notes) == 1 else 'notes'}: {reworded} reviewer "
            f"{'note' if reworded == 1 else 'notes'} reworded by {notes_provider} with figures and route checked, "
            f"{len(run.notes) - reworded} from templates; {count} {'letter' if count == 1 else 'letters'} to clients from templates "
            "(letters are never reworded)"
        )
    emit(LaneResult("notes", "done", (perf_counter() - started) * 1000, notes_summary,
                    detail={"notes": dict(run.notes), "sources": dict(run.note_sources), "letters": dict(run.letters)}))

    run.state = _audit(
        applied,
        run.change,
        "applied",
        preview.detail,
        pack=applied.pack,
        approver=name,
        facts=preview.facts,
        materiality=preview.materiality,
        parity=preview.parity,
    )
    run.applied = True
    return run


def impact_rows(items: list[ReturnImpact]) -> list[dict]:
    """Flat rows for tables and JSON output."""
    rows = []
    for item in items:
        rows.append(
            {
                "client": item.client_id,
                "status": item.status,
                "route": item.route,
                "lines": ", ".join(diff.line for diff in item.diffs if diff.line not in ("magi", "tax_formula")),
                "tax_change": None if item.delta_tax is None else float(item.delta_tax),
                "vs_filed": None if item.filed_delta_tax is None else float(item.filed_delta_tax),
                "tax_range": None if item.tax_range is None else [float(end) for end in item.tax_range],
                "vs_filed_range": None if item.filed_range is None else [float(end) for end in item.filed_range],
                "needs": "; ".join(item.needs),
            }
        )
    return rows


def run_json(run: Run) -> dict[str, Any]:
    """One run as `taxdiff run --json` prints it. The web API returns the same object."""
    shown = run.applied or run.pending is not None
    report = run.report
    return {
        "change": run.change.id,
        "applied": run.applied,
        "previewed": run.pending is not None,
        "errors": [str(e) for e in (run.validation.errors if run.validation else [])],
        "lanes": [{"lane": lane.lane, "state": lane.state, "ms": round(lane.ms, 1), "summary": lane.summary} for lane in run.lanes],
        "reached": impact_rows(report.reached) if report and shown else [],
        "unexpected": report.unexpected if report else [],
        "tax_after": {cid: str(r.values["tax"]) for cid, r in sorted(report.results.items())} if report and shown else {},
    }
