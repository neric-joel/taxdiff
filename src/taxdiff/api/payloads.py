"""JSON for the dashboard: book rows, the impact list with provenance chains, the not-reached list, notes.

Every amount carries the exact value as a string and the text the command line prints for it, so the
screen shows the same figures as `taxdiff run --json`. Nothing here computes a tax amount: values come
from Engine A's results, and the only arithmetic is the sum of the per-return changes Engine A reported.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from ..changes import ChangeSet, Validation
from ..engine import LINE_NUMBERS, LINES, ComputedReturn
from ..facts import Book, ClientReturn, fact_document, fact_label, parse_path
from ..impact import ImpactReport, ReturnImpact, delta
from ..lanes import law
from ..lanes.engine_b import LaneOutput, Parity
from ..lanes.explain import Answer, fact_text, new_value, rule_value
from ..lanes.notes import money, signed, span
from ..provenance import Unknown, Value
from ..provisions import OVERTIME
from ..router import ROUTES
from ..rules import LABELS, FieldError, RulePack, value_words
from ..workflow import AuditEvent, State, pack_digest
from .session import Sources

SHOWN_LINES = [line for line in LINES if line not in ("magi", "tax_formula")]


def amount(value: Value | None, *, sign: bool = False) -> dict[str, Any]:
    if value is None:
        return {"value": None, "text": ""}
    if isinstance(value, Unknown):
        return {"value": None, "text": "unknown", "unknown": True, "needs": list(value.needs)}
    return {"value": str(value), "text": signed(value) if sign else money(value)}


def ranged(value: Decimal | None, pair: tuple[Decimal, Decimal] | None, needs: Iterable[str] = ()) -> dict[str, Any]:
    """A tax change, or while a document is outstanding the range Engine A bounded it to."""
    if value is None and pair is not None:
        return {"value": None, "text": span(pair), "unknown": True, "needs": list(needs), "range": [str(pair[0]), str(pair[1])]}
    return amount(value, sign=True)


def tax_change_of(item: ReturnImpact) -> dict[str, Any]:
    return ranged(item.delta_tax, item.tax_range, item.needs)


def vs_filed_of(item: ReturnImpact) -> dict[str, Any]:
    return ranged(item.filed_delta_tax, item.filed_range, item.needs)


def delta_of(before: Value | None, after: Value | None) -> Decimal | None:
    return delta(before, after)


def _delta(before: Value, after: Value) -> dict[str, Any] | None:
    change = delta(before, after)
    return None if change is None else amount(change, sign=True)


def pack_view(pack: RulePack) -> dict[str, Any]:
    definition = pack.value("overtime.definition")
    return {"name": pack.name, "version": pack.version, "sha": pack_digest(pack), "tax_year": pack.tax_year,
            "overtime_definition": definition, "overtime_label": value_words("overtime.definition", definition),
            "overtime_summary": OVERTIME[definition].summary}


def facts_versions(book: Book) -> dict[str, int]:
    """Clients whose facts moved past version 1."""
    return {ret.client_id: ret.facts_version for ret in book.returns if ret.facts_version > 1}


def fact_value(ret: ClientReturn, path: str) -> Any:
    if path == "people":
        return len(ret.people)
    target: Any = ret.model_dump(mode="json")
    for part in parse_path(path):
        target = target[part]
    return target


def parity_view(parity: Parity) -> dict[str, Any]:
    return {
        "state": parity.state,
        "reason": parity.reason,
        "authority": parity.authority,
        "diffs": [{"line": line, "label": LINES[line], "no": LINE_NUMBERS[line], "engine_a": money(ours), "policyengine": money(theirs),
                   "difference": money(abs(delta(theirs, ours) or 0))} for line, ours, theirs in parity.diffs],
    }


def book_rows(state: State, routes: Mapping[str, str], cases: Mapping[str, str]) -> list[dict[str, Any]]:
    rows = []
    for client_id, result in sorted(state.results.items()):
        ret = state.book.get(client_id)
        v = result.values
        rows.append({
            "client": client_id,
            "status": result.status,
            "filing": result.filing_status,
            "agi": amount(v["agi"]),
            "sch1a_total": amount(v["sch1a_total"]),
            "taxable_income": amount(v["taxable_income"]),
            "tax": amount(v["tax"]),
            "as_filed_tax": amount(state.as_filed[client_id]["tax"]) if client_id in state.as_filed else None,
            "facts_version": ret.facts_version if ret else 1,
            "parity": state.parity[client_id].state,
            "route": routes.get(client_id, ""),
            "case": cases.get(client_id, ""),
        })
    return rows


def _read(path: str, client_id: str, ret: ClientReturn, pack: RulePack, sources: Sources, touched: set[str]) -> dict[str, Any]:
    kind, _, name = path.partition(":")
    if kind == "rule":
        return {"kind": "rule", "name": name, "label": LABELS.get(name, name), "value": rule_value(pack, name), "cite": pack.cite(name), "url": pack.url(name),
                "set_by": sources.rules.get(name), "touched": path in touched}
    if kind == "fact":
        value = fact_value(ret, name)
        source = sources.facts.get((client_id, name))
        return {"kind": "fact", "name": name, "label": fact_label(name), "value": fact_text(name, value),
                "source": source or {"text": f"{fact_document(name)}, client facts version {ret.facts_version}"}, "touched": path in touched}
    definition, _, version = name.partition("@")
    key = definition.split(".", 1)[-1]
    return {"kind": "definition", "name": definition, "version": version,
            "summary": OVERTIME[key].summary if key in OVERTIME else "", "touched": False}


def chain(result: ComputedReturn, line: str, ret: ClientReturn, pack: RulePack, sources: Sources, touched: set[str]) -> dict[str, Any]:
    """The line, what it read directly, and the earlier lines it used, each once: the provenance chain."""
    seen: set[str] = set()

    def node(name: str) -> dict[str, Any]:
        seen.add(name)
        used = sorted(result.uses.get(name, ()), key=list(LINES).index)
        inherited: set[str] = set().union(*(result.reads[u] for u in used)) if used else set()
        direct = sorted(result.reads[name] - inherited)
        return {
            "line": name,
            "label": LINES[name],
            "no": LINE_NUMBERS[name],
            "value": amount(result.values[name]),
            "reads": [_read(path, result.client_id, ret, pack, sources, touched) for path in direct],
            "uses": [node(u) for u in used if u not in seen],
        }

    return node(line)


def _touched_paths(touched: Iterable[tuple[str | None, str]], client_id: str) -> set[str]:
    return {path for client, path in touched if client is None or client == client_id}


def _line_citation(change: ChangeSet, line: str, reads: frozenset[str]) -> dict[str, Any]:
    """The source to show beside a changed line: the citations of the edited rules this line read, else the
    document, else the line's own authority."""
    if change.kind == "rule" and change.rule_edits:
        used = [edit for edit in change.rule_edits if f"rule:{edit.path}" in reads] or change.rule_edits
        return {"text": "; ".join(dict.fromkeys(edit.cite for edit in used)), "url": next((e.url for e in used if e.url), None), "by": change.id}
    if change.kind == "document":
        return {"text": f"Employer overtime statement, {change.document} (SYNTHETIC)", "url": None, "by": change.id}
    found = law.for_lines([line])
    return {"text": found[0].title, "url": found[0].url, "by": change.id} if found else {"text": change.title, "url": None, "by": change.id}


SCHEDULE_1A = ("sch1a_overtime", "sch1a_tips", "sch1a_senior")
AMENDED_LINES = (("agi", "11b"), ("standard_deduction", "12e"), ("sch1a_total", "13b"), ("taxable_income", "15"), ("tax", "16"))


def _step_value(value: Any) -> str:
    if isinstance(value, Unknown):
        return "unknown"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return money(value)


def worksheet(result: ComputedReturn, lines: Iterable[str]) -> list[dict[str, str]]:
    """The worksheet steps the engine recorded for these lines, as text."""
    return [{"line": line, "ref": ref, "label": label, "value": _step_value(value)}
            for line in lines for ref, label, value in result.work.get(line, ())]


def method(result: ComputedReturn) -> str:
    """How the overtime deduction was figured: the Notice 2025-69 method and its arithmetic, then the cap, the
    phase-out step or the separate-return rule when one applies; or ""."""
    work = result.work.get("sch1a_overtime", ())
    steps = [f"{label}: {_step_value(value)}" for ref, label, value in work if ref.startswith("14") and ref != "14c"]
    by_ref = {ref: (label, value) for ref, label, value in work}
    qualified, capped = by_ref.get("14c", ("", None))[1], by_ref.get("15", ("", None))[1]
    if isinstance(qualified, Decimal) and isinstance(capped, Decimal) and capped < qualified:
        steps.append(f"capped at {money(capped)}")
    reduction = by_ref.get("20", ("", None))[1]
    if isinstance(reduction, Decimal) and reduction > 0:
        steps.append(f"phase-out: modified AGI {money(by_ref['16'][1])} is {money(by_ref['18'][1])} over {money(by_ref['17'][1])}, "
                     f"{by_ref['19'][1]} whole $1,000 {'step' if by_ref['19'][1] == 1 else 'steps'}, minus {money(reduction)}")
    if "21" in by_ref and by_ref["21"][0].startswith("Married filing separately"):
        steps.append(by_ref["21"][0])
    return "; ".join(steps)


def amended(ret: ClientReturn | None, result: ComputedReturn) -> list[dict[str, str]] | None:
    """A corrected-return preview for a filed return: each line as filed, the change, and the corrected value."""
    if ret is None or ret.status != "filed" or not ret.as_filed:
        return None
    rows = []
    for line, no in AMENDED_LINES:
        filed, now = ret.as_filed.get(line), result.values[line]
        if filed is None:
            continue
        rows.append({"line": no, "label": LINES[line].split(",")[0], "as_filed": money(filed),
                     "change": "unknown" if isinstance(now, Unknown) else signed(delta(filed, now)), "corrected": _step_value(now)})
    return rows


_STATUS_WORDS = {"prep": "being prepared", "review": "in review", "filed": "already filed"}
_FILING_WORDS = {"single": "single", "mfj": "joint", "mfs": "married-filing-separately", "hoh": "head-of-household"}


def story(report: ImpactReport, change: ChangeSet, paths: set[tuple[str | None, str]], *, applied: bool = False) -> dict[str, Any] | None:
    """One reached return, start to finish, in plain words: what moved, by how much, and what happens next. The
    return is the filed one with the largest change against what it was filed with, else the one whose tax moves
    most, else one that waits for a document. Every figure comes from the run."""
    moved = [item for item in report.reached if item.delta_tax]
    filed = [item for item in report.moved_filed() if item.filed_delta_tax]
    waiting = [item for item in report.reached if item.waiting or item.conflict]
    pick = (max(filed, key=lambda i: abs(i.filed_delta_tax or 0)) if filed else max(moved, key=lambda i: abs(i.delta_tax or 0)) if moved
            else waiting[0] if waiting else None)
    if pick is None:
        return None
    line = primary_line(pick, _touched_paths(paths, pick.client_id))
    goes, gets, asked = ("goes", "gets", "is asked") if applied else ("would go", "would get", "would be asked")
    who = f"{pick.client_id}, a {_FILING_WORDS.get(pick.after.filing_status, pick.after.filing_status)} return {_STATUS_WORDS[pick.status]}"
    parts = [f"{who}."]
    if line is not None:
        diff = next(d for d in pick.diffs if d.line == line)
        parts.append(f"Its {LINES[line].split(',')[0].lower()} {goes} from {amount(diff.before)['text']} to {amount(diff.after)['text']}.")
    if pick.conflict:
        parts.append(pick.conflict)
    own, filed = pick.delta_tax, pick.filed_delta_tax
    if pick.status == "filed" and filed and own and own != filed:
        parts.append(f"This change moves its tax {'up' if own > 0 else 'down'} {money(abs(own))}; against the filed return it is "
                     f"{'up' if filed > 0 else 'down'} {money(abs(filed))} in all.")
    elif pick.status == "filed" and filed:
        parts.append(f"Its tax {goes} {'up' if filed > 0 else 'down'} {money(abs(filed))} against the filed return.")
    elif own:
        parts.append(f"Its tax {goes} {'up' if own > 0 else 'down'} {money(abs(own))}.")
    elif pick.tax_range:
        parts.append(f"Until the client's document arrives, its tax change is between {signed(pick.tax_range[0])} and {signed(pick.tax_range[1])}.")
    nxt = ROUTES[pick.route].split(":")[0].lower()
    letter = f" The client {gets} a letter that explains it." if pick.status == "filed" and pick.route in ("amendment", "below_materiality") else (
        f" The client {asked} for the missing document." if pick.waiting else "")
    parts.append(f"Next step: {nxt}.{letter}")
    return {"client": pick.client_id, "text": " ".join(parts), "change": change.id}


def primary_line(item: ReturnImpact, paths: Iterable[str]) -> str | None:
    """The line to show first for a reached return: the first moved line that reads an edited rule or fact
    itself (wages for a corrected W-2, the overtime deduction for an overtime rule), else the first moved
    Schedule 1-A line, else the first moved line."""
    moved = [d.line for d in item.diffs if d.line not in ("magi", "tax_formula")]
    edited = set(paths)
    for line in moved:
        for result in (item.after, item.before):
            # a line's reads include what the lines it used read; its own reads are the rest
            inherited = set().union(*(result.reads.get(used, frozenset()) for used in result.uses.get(line, frozenset())))
            if edited & (set(result.reads.get(line, frozenset())) - inherited):
                return line
    return next((line for line in moved if line in SCHEDULE_1A), moved[0] if moved else None)


def route_reason(item: ReturnImpact, parity: Parity | None, materiality: Decimal | None) -> str:
    """Why the return got its route, in a few words."""
    if item.conflict:
        source = "a new statement contradicts" if item.conflict.startswith("The statement") else "corrected facts contradict"
        return f"{source} the {'return as filed' if item.status == 'filed' else 'prepared return'}"
    if item.route == "no_change":
        return "the change reaches it but moves no line" if not item.needs else "still waiting for an earlier request; this change moves no line"
    if item.needs:
        # the row already says it waits for a document, and that it is filed
        return "amendment review as well" if item.status == "filed" else "waits for a document"
    if parity is not None and parity.state == "differs":
        return "the cross-check differs"
    if item.status in ("prep", "review"):
        return ""  # the row shows the status already
    limit = money(materiality) if materiality is not None else "the materiality"
    if item.route == "below_materiality":
        return f"moves less than {limit} vs. the filed return"
    return f"moves {limit} or more vs. the filed return"


def impact_item(item: ReturnImpact, change: ChangeSet, *, book: Book, pack: RulePack, parity: Mapping[str, Parity],
                sources: Sources, touched: set[tuple[str | None, str]], materiality: Decimal | None = None) -> dict[str, Any]:
    ret = book.get(item.client_id)
    assert ret is not None
    paths = _touched_paths(touched, item.client_id)
    changed = [d.line for d in item.diffs if d.line in SCHEDULE_1A]
    lines = []
    for diff in item.diffs:
        if diff.line in ("magi", "tax_formula"):
            continue
        lines.append({
            "line": diff.line,
            "label": LINES[diff.line],
            "no": LINE_NUMBERS[diff.line],
            "before": amount(diff.before),
            "after": amount(diff.after),
            "delta": _delta(diff.before, diff.after),
            "citation": _line_citation(change, diff.line, item.after.reads[diff.line] | item.before.reads[diff.line]),
            "chain": chain(item.after, diff.line, ret, pack, sources, paths),
        })
    return {
        "client": item.client_id,
        "status": item.status,
        "filing": item.after.filing_status,
        "route": item.route,
        "route_label": ROUTES.get(item.route, ""),
        "tax_before": amount(item.before.values["tax"]),
        "tax_after": amount(item.after.values["tax"]),
        "tax_change": tax_change_of(item),
        "vs_filed": vs_filed_of(item) if item.status == "filed" else None,
        "parity": parity_view(parity[item.client_id]) if item.client_id in parity else None,
        "needs": list(item.needs),
        "lines": lines,
        "touched": sorted(paths),
        "method": method(item.after),
        "method_before": method(item.before),
        "worksheet": {"before": worksheet(item.before, changed), "after": worksheet(item.after, changed)} if changed else None,
        "amended": amended(ret, item.after),
        "primary_line": primary_line(item, paths),
        "conflict_change": amount(item.conflict_delta, sign=True) if item.conflict_delta is not None else None,
        "route_reason": route_reason(item, parity.get(item.client_id), materiality),
        "conflict": item.conflict or None,
    }


def not_reached_rows(report: ImpactReport, before: Mapping[str, ComputedReturn], touched: set[tuple[str | None, str]]) -> list[dict[str, Any]]:
    """Each return the change did not reach, and why: from its recorded reads."""
    rule_keys = sorted(path.removeprefix("rule:") for client, path in touched if client is None and path.startswith("rule:"))
    clients = sorted({client for client, _ in touched if client})
    prefixes = sorted({key.split(".")[0] for key in rule_keys})
    rows = []
    for client_id in report.not_reached:
        result = before[client_id]
        if rule_keys:
            related = sorted({read.removeprefix("rule:") for read in result.all_reads
                              if read.startswith("rule:") and read.removeprefix("rule:").split(".")[0] in prefixes})
            # Name the closest rules it does use (the joint cap when the single cap changed), not its whole family.
            stems = {".".join(k.split(".")[:2]) for k in rule_keys}
            near = [r for r in related if ".".join(r.split(".")[:2]) in stems]
            edited = ", the ".join(LABELS.get(k, k) for k in rule_keys)
            if near:
                reason = f"uses the {', the '.join(LABELS.get(r, r) for r in near)}, not the {edited}"
            elif related:
                reason = f"uses other {' and '.join(p.replace('_', ' ') for p in prefixes)} rules, not the {edited}"
            else:
                reason = f"uses no {' or '.join(p.replace('_', ' ') for p in prefixes)} rule"
        else:
            reason = f"only {', '.join(clients)} changed; no line on this return uses those facts"
        rows.append({"client": client_id, "status": result.status, "reason": reason, "moved": client_id in report.unexpected})
    return rows


def kpis(report: ImpactReport, parity: Mapping[str, Parity]) -> dict[str, Any]:
    total, unknown = report.tax_change()
    filed = report.filed_change()
    routes = {key: sum(item.route == key for item in report.reached) for key in ROUTES}
    reached_ids = {item.client_id for item in report.reached}
    count = lambda ids: {s: sum(parity[c].state == s for c in ids if c in parity) for s in ("agree", "differs", "not_checked")}  # noqa: E731
    return {
        "reached": len(report.reached),
        "not_reached": len(report.not_reached),
        "unexpected": len(report.unexpected),
        "total": len(report.results),
        "tax_change": amount(total, sign=True) if total is not None else None,
        "tax_change_unknown": unknown,
        "vs_filed": amount(filed, sign=True) if filed is not None else None,
        # gross figures, so a refund on one return never hides more tax on others
        "moves": {key: {"returns": n, "amount": amount(total_of, sign=True)} for key, (n, total_of) in report.tax_moves().items()},
        "filed_moves": {key: {"returns": n, "amount": amount(total_of, sign=True)} for key, (n, total_of) in report.filed_moves().items()},
        # returns whose change waits on a document, with the sum of their ranges when every one has a range
        "unknown_moves": _unknown_view(report.unknown_moves()),
        # returns whose tax was unknown before this change and is known now (a document arrived)
        "settled": _settled_view(report),
        # conflicts priced under the Notice's rule while the active rules still count all overtime pay
        "conflict_moves": _conflict_view(report),
        "differs": [{"client": item.client_id, "line": line, "label": LINES[line], "no": LINE_NUMBERS[line], "engine_a": money(ours),
                     "policyengine": money(theirs), "difference": money(abs(delta(theirs, ours) or 0)),
                     "reason": parity[item.client_id].reason, "authority": parity[item.client_id].authority}
                    for item in report.reached if item.client_id in parity and parity[item.client_id].state == "differs"
                    for line, ours, theirs in parity[item.client_id].diffs],
        "routes": routes,
        "parity": count(parity.keys()),
        "parity_reached": count(reached_ids),
    }


def _conflict_view(report: ImpactReport) -> dict[str, Any]:
    priced = [item for item in report.reached if item.conflict and item.conflict_delta is not None]
    total = sum((item.conflict_delta for item in priced if item.conflict_delta is not None), Decimal(0))
    return {"returns": len(priced), "clients": [item.client_id for item in priced], "amount": amount(total, sign=True) if priced else None}


def _settled_view(report: ImpactReport) -> dict[str, Any]:
    settled = report.settled()
    filed = [item.filed_delta_tax for item in settled if item.status == "filed" and item.filed_delta_tax is not None]
    return {"returns": len(settled), "clients": [item.client_id for item in settled],
            "vs_filed": amount(sum(filed, Decimal(0)), sign=True) if filed else None}


def _unknown_view(moves: Mapping[str, Any]) -> dict[str, Any]:
    whole = moves["returns"] and moves["ranged"] == moves["returns"]
    return {"returns": moves["returns"],
            "range": ranged(None, (moves["low"], moves["high"])) if whole else None}


def change_view(change: ChangeSet, pack_before: RulePack, book_before: Book, citations: Mapping[str, Any] | None = None) -> dict[str, Any]:
    edits = []
    for edit in change.rule_edits:
        edits.append({"kind": "rule", "path": edit.path, "label": LABELS.get(edit.path, edit.path), "check": (citations or {}).get(edit.path),
                      "from": rule_value(pack_before, edit.path) if edit.path in pack_before.parameters else None,
                      "to": new_value(edit.path, edit.to), "cite": edit.cite, "url": edit.url})
    for edit in change.fact_edits:
        ret = book_before.get(edit.client_id)
        try:
            old = fact_value(ret, edit.path) if ret else None
        except (KeyError, IndexError, ValueError, TypeError):
            old = None
        edits.append({"kind": "fact", "client": edit.client_id, "path": edit.path, "label": fact_label(edit.path),
                      "from": fact_text(edit.path, old), "to": fact_text(edit.path, edit.to)})  # "not on file" when empty
    return {"id": change.id, "kind": change.kind, "title": change.title, "author": change.author, "rationale": change.rationale,
            "document": change.document, "client": change.client_id, "edits": edits}


def facts_before(change: ChangeSet, book: Book) -> dict[str, dict[str, Any]]:
    """The values a fact change replaces, for the answer text."""
    out: dict[str, dict[str, Any]] = {}
    for edit in change.fact_edits:
        ret = book.get(edit.client_id)
        if ret is None:
            continue
        try:
            out.setdefault(edit.client_id, {})[edit.path] = fact_value(ret, edit.path)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


def authorities(items: Iterable[law.Authority]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def audit(events: Iterable[AuditEvent]) -> list[dict[str, Any]]:
    return [asdict(event) for event in events]


def answer(item: Answer) -> dict[str, Any]:
    return {"text": item.text, "links": item.links, "source": item.source, "headline": item.headline,
            "headline_links": item.headline_links, "sections": item.sections, "kind": item.kind}


def error_items(validation: Validation | None) -> list[FieldError]:
    """Field-level reasons as validation gave them. An unexpected error, which can carry internal text, is
    reported by its kind only."""
    out = []
    for error in validation.errors if validation else []:
        if error.field in ("run", "engine_a") and "(" in error.message:
            kind = error.message.split("(", 1)[1].split(":", 1)[0].split(")", 1)[0]
            out.append(FieldError(error.field, f"stopped by an unexpected error ({kind}); nothing was applied"))
        else:
            out.append(error)
    return out


def errors(validation: Validation | None) -> list[dict[str, str]]:
    return [{"field": e.field, "message": e.message} for e in error_items(validation)]


# Node payloads for the drawer: what each lane read and what it produced -----------------------------------


def lane_payload(name: str, detail: Mapping[str, Any] | None, *, change: ChangeSet, state: State) -> tuple[Any, Any]:
    """(input, output) for one workflow lane, from the objects the lane reported."""
    detail = detail or {}
    if name == "documents":
        statement = detail.get("statement")
        out = {"method": statement.method, "fields": statement.fields, "edits": [e.model_dump() for e in statement.edits]} if statement else {}
        if detail.get("errors"):
            out["errors"] = [str(e) for e in detail["errors"]]
        return {"document": change.document, "client": change.client_id}, out
    if name == "validate":
        validation: Validation | None = detail.get("validation")
        checked: ChangeSet = detail.get("change", change)
        out = {"ok": bool(validation and validation.ok), "errors": errors(validation)}
        if validation and validation.ok:
            out["next_pack_version"] = validation.pack.version if validation.pack else None
            out["touched"] = sorted(f"{client or 'every client'}: {path}" for client, path in validation.touched)
        return {"change": checked.model_dump(mode="json", exclude_defaults=True), "pack_version": state.pack.version}, out
    if name == "engine_a":
        report: ImpactReport | None = detail.get("report")
        if report is None:
            return None, None
        return ({"returns": len(state.results), "pack_version": state.pack.version},
                {"reached": [item.client_id for item in report.reached], "not_reached": len(report.not_reached),
                 "unexpected": report.unexpected, "book_recompute_ms": round(report.ms, 2)})
    if name == "engine_b":
        output: LaneOutput | None = detail.get("output")
        if output is None:
            return None, None
        return ({"mode": output.source},
                {"release": output.version, "checked": len(output.results), "not_checked": dict(list(output.skipped.items())[:12]),
                 "note": output.note})
    if name == "law":
        items = detail.get("authorities") or []
        return ({"rule_keys": [e.path for e in change.rule_edits]},
                {"authorities": [{"title": a.title, "url": a.url, "fetched": a.fetched, "as_of": a.as_of, "fallback": a.fallback} for a in items]})
    if name == "route":
        report = detail.get("report")
        parity = detail.get("parity") or {}
        if report is None:
            return None, None
        return ({"materiality": str(detail.get("materiality")), "engines_differ": sorted(c for c, p in parity.items() if p.state == "differs")},
                {"routes": {item.client_id: item.route for item in report.reached}, "counts": report.counts()})
    if name == "notes":
        notes = detail.get("notes") or {}
        sources = detail.get("sources") or {}
        return None, {client: {"text": text, "source": sources.get(client, "")} for client, text in notes.items()}
    if name == "approve":
        report = detail.get("report")
        return None, ({"reached": len(report.reached), "routes": report.counts()} if report else None)
    return None, None

