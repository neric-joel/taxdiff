"""Explain lane: answer the reviewer's question from the run, with every figure inserted by code.

An answer is a headline sentence that states the result, then sections built by code: fact rows, short
lists of returns to act on, and short paragraphs. Every figure in them carries a link to the client row and
line it came from. The answer is built from Engine A's results: the change and its reach for a change, or a
client's line values, the approved changes that moved them, and the line's inputs for a question about a
client. An optional LLM may reword the headline into a direct answer to the question; the rewording is kept
only if it carries exactly the headline's figures in the same order and still names every client and change
id. The sections are never reworded. The whole answer is also kept as one paragraph, for the download and the
command line.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..changes import ChangeSet
from ..engine import LINES, bounds
from ..facts import fact_label
from ..impact import ReturnImpact, delta
from ..provenance import Unknown, Value
from ..provisions import OVERTIME
from ..router import ROUTES
from ..rules import LABELS, PAIRS, SCHEMA, RulePack, bounded, value_words
from .notes import _FIGURE, figures, money, signed, span

Link = dict[str, str]
Piece = str | tuple[str, Link | None]  # plain text, or text with the row (or question) it links to


@dataclass
class Answer:
    """The answer. text is the whole answer as one paragraph and links gives, for each figure in it (in the
    order notes.figures reads them), its link or None. headline and headline_links do the same for the first
    sentence, the only part a model may reword. sections are built by code: each is a paragraph, a list of
    fact rows or a list of items, made of parts that carry their own link."""

    text: str
    links: list[Link | None] = field(default_factory=list)
    ids: tuple[str, ...] = ()  # client and change ids a rewording must keep
    source: str = "template"
    headline: str = ""
    headline_links: list[Link | None] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    kind: str = ""


class _Builder:
    def __init__(self) -> None:
        self.parts: list[tuple[str, Link | None]] = []

    def add(self, text: str, link: Link | None = None) -> _Builder:
        self.parts.append((text, link))
        return self

    def answer(self) -> Answer:
        text = "".join(part for part, _ in self.parts)
        bounds, offset = [], 0
        for part, link in self.parts:
            bounds.append((offset, offset + len(part), link))
            offset += len(part)
        links = []
        for match in _FIGURE.finditer(text):
            links.append(next((link for start, end, link in bounds if start <= match.start() < end), None))
        ids = tuple(dict.fromkeys(re.findall(r"\bCS?-\d{3}\b", text)))
        return Answer(re.sub(r"\s+", " ", text).strip(), links, ids)


def _parts(pieces: Iterable[Piece]) -> list[dict[str, Any]]:
    out = []
    for piece in pieces:
        text, target = (piece, None) if isinstance(piece, str) else piece
        if text:
            out.append({"text": text, "target": target})
    return out


class _View:
    """An answer as a headline and sections, flattened into one paragraph for its text form."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.headline: list[dict[str, Any]] = []
        self.sections: list[dict[str, Any]] = []

    def head(self, *pieces: Piece) -> _View:
        self.headline += _parts(pieces)
        return self

    def para(self, *pieces: Piece) -> _View:
        self.sections.append({"type": "paragraph", "content": _parts(pieces)})
        return self

    def facts(self, rows: Iterable[tuple[str, Piece | list[Piece]]], title: str | None = None) -> _View:
        rows = [{"label": label, "value": _parts(value if isinstance(value, list) else [value])} for label, value in rows]
        if rows:
            self.sections.append({"type": "facts", "title": title, "rows": rows})
        return self

    def items(self, items: Iterable[list[Piece]], title: str | None = None) -> _View:
        listed = [_parts(item) for item in items]
        if listed:
            self.sections.append({"type": "list", "title": title, "items": listed})
        return self

    def answer(self) -> Answer:
        whole, head = _Builder(), _Builder()
        for part in self.headline:
            whole.add(part["text"], part["target"])
            head.add(part["text"], part["target"])
        for section in self.sections:
            whole.add(" ")
            if section["type"] == "paragraph":
                for part in section["content"]:
                    whole.add(part["text"], part["target"])
                continue
            if section["title"]:
                whole.add(f"{section['title']}: ")
            entries = [[{"text": f"{row['label']} ", "target": None}, *row["value"]] for row in section["rows"]] if section["type"] == "facts" else section["items"]
            for n, entry in enumerate(entries):
                whole.add("; " if n else "")
                for part in entry:
                    whole.add(part["text"], part["target"])
            whole.add(".")
        full, first = whole.answer(), head.answer()
        return Answer(full.text, full.links, full.ids, "template", first.text, first.links, self.sections, self.kind)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def rule_value(pack: RulePack, key: str) -> str:
    value = pack.value(key)
    kind = SCHEMA.get(key, "")
    if kind == "money" or key.endswith(".phaseout.step") or key == "tax_table.below":  # dollar steps and limits
        return money(value)
    if kind == "brackets":
        return "the rate schedule"
    return value_words(key, value)


def new_value(path: str, to: Any) -> str:
    """A rule edit's new value as the answer and the approval panel show it."""
    if SCHEMA.get(path) == "money" and _is_number(to):
        try:
            return money(bounded(Decimal(str(to).replace(",", ""))))
        except ValueError:
            return str(to)
    return value_words(path, to)


_MONEY_FACTS = {"wages", "ot_box14", "ot_premium_reported", "ot_premium_paid", "tips_qualified", "ot_regular_rate"}
_BASIS = {"flsa": "FLSA overtime", "state_or_contract": "state law or a contract only", "mixed": "FLSA and state-law overtime, not split"}
_FILING = {"single": "single", "mfj": "married filing jointly", "mfs": "married filing separately", "hoh": "head of household"}


def fact_text(path: str, value: Any) -> str:
    """A fact value in plain words: amounts as money, yes or no for a flag, "empty" when none is on file."""
    if value is None:
        return "not on file"
    if isinstance(value, bool):
        return "yes" if value else "no"
    name = path.rsplit(".", 1)[-1].split("[", 1)[0]
    if name in _MONEY_FACTS and _is_number(value):
        try:
            return money(bounded(Decimal(str(value).replace(",", ""))))
        except ValueError:
            return str(value)
    if name == "ot_flsa_hours" and _is_number(value):
        return f"{value} hours"
    if name == "ot_basis":
        return _BASIS.get(str(value), str(value))
    if name == "ot_multiple" and _is_number(value):
        return f"{Decimal(str(value)):g}x the regular rate"
    if name == "filing_status":
        return _FILING.get(str(value), str(value))
    return str(value)


def _route(item: ReturnImpact) -> str:
    return ROUTES[item.route].lower()


def _is_number(value: Any) -> bool:
    try:
        Decimal(str(value).replace(",", ""))
    except ArithmeticError:
        return False
    except ValueError:
        return False
    return True


# A cap a rule edit sets, and the Schedule 1-A line it limits.
_CAPPED = (("overtime.cap", "sch1a_overtime"), ("tips.cap", "sch1a_tips"))


def _largest_capped(change: ChangeSet, reached: list[ReturnImpact]) -> tuple[str, str, Decimal, Decimal] | None:
    """For a what-if that lowers or raises a cap and moves no return: the capped line, the reached return with the
    largest value on it, that value, and the new cap. None for any other change."""
    for edit in change.rule_edits:
        line = next((line for prefix, line in _CAPPED if edit.path.startswith(prefix)), None)
        if line is None or isinstance(edit.to, bool):
            continue
        try:
            cap = Decimal(str(edit.to))
        except ArithmeticError:
            continue
        values = [(item.client_id, value) for item in reached if isinstance(value := item.after.values.get(line), Decimal)]
        if values:
            client, value = max(values, key=lambda pair: (pair[1], pair[0]))
            return line, client, value, cap
    return None


def _edit_rows(change: ChangeSet, pack_before: RulePack, facts_before: Mapping[str, Mapping[str, Any]], *, cite: bool) -> list[tuple[str, list[Piece]]]:
    """One row per edit, old value to new; for a rule edit, the filing-status twin it leaves as it was, and its
    authority."""
    rows: list[tuple[str, list[Piece]]] = []
    if change.kind == "rule":
        edited = {edit.path for edit in change.rule_edits}
        for edit in change.rule_edits:
            old = rule_value(pack_before, edit.path) if edit.path in pack_before.parameters else "not set"
            rows.append((_cap(LABELS.get(edit.path, edit.path)), [f"{old} to {new_value(edit.path, edit.to)}"]))
        for path in dict.fromkeys(PAIRS[e] for e in edited if e in PAIRS):
            if path not in edited and path in pack_before.parameters:
                rows.append((_cap(LABELS.get(path, path)), [f"{rule_value(pack_before, path)}, unchanged"]))
        if cite:
            for source in dict.fromkeys(edit.cite for edit in change.rule_edits if edit.cite):
                rows.append(("Authority", [source]))
        return rows
    for edit in change.fact_edits:
        before = fact_text(edit.path, facts_before.get(edit.client_id, {}).get(edit.path))
        rows.append((f"{edit.client_id}, {fact_label(edit.path)}", [f"{before} to {fact_text(edit.path, edit.to)}"]))
    return rows


def change_answer(
    change: ChangeSet,
    report: Any,
    pack_before: RulePack,
    facts_before: Mapping[str, Mapping[str, Any]],
    *,
    decision: str,
    approver: str = "",
    hypothetical: bool = False,
) -> Answer:
    """The answer for a change: the result first (reach and tax change, or for a what-if the returns whose
    tax would move), then what the change sets, the returns that need someone outside the firm or an
    amendment, and the decision.

    decision is "approved", "turned down", "what-if" (a preview that is never applied) or "preview".
    """
    v = _View("what_if" if decision == "what-if" else "change")
    reached, total = report.reached, len(report.results)
    total_change, unknown = report.tax_change()
    movers = sorted((item for item in reached if item.delta_tax), key=lambda item: (-abs(item.delta_tax), item.client_id))
    edited = "rule" if change.kind == "rule" else "facts"

    def tax(item: ReturnImpact) -> Piece:
        return (signed(item.delta_tax), {"client": item.client_id, "line": "tax"})

    def who(item: ReturnImpact) -> Piece:
        return (item.client_id, {"client": item.client_id})

    if decision == "what-if":
        moves = report.tax_moves()
        (up, up_sum), (down, down_sum) = moves["more"], moves["less"]
        if len(movers) == 1:
            v.head(who(movers[0]), "'s tax would change by ", tax(movers[0]), ".")
        elif up and down:
            v.head(f"{up} {'return' if up == 1 else 'returns'} would owe more tax (", (signed(up_sum), None), f" in all) and {down} less (",
                   (signed(down_sum), None), ").")
        elif up or down:
            n, amount_ = (up, up_sum) if up else (down, down_sum)
            v.head(f"{n} returns would owe {'more' if up else 'less'} tax, ", (signed(amount_), None), " in all.")
        else:
            v.head("No return's tax would change.")
        v.para("Nothing was applied: a what-if is a preview only.")
        v.facts(_edit_rows(change, pack_before, facts_before, cite=False), "What it would change")
        v.items(([who(item), " ", tax(item), f" ({_route(item)})"] for item in movers[:5]), "Returns whose tax would change")
        still = sum(1 for item in reached if item.route == "no_change")
        other = len(report.not_reached)
        rest = f"The other {other} {'return uses' if other == 1 else 'returns use'} no rule or fact this change edits."
        largest = _largest_capped(change, reached) if reached and still == len(reached) else None
        if largest is not None:
            line, item, value, cap = largest
            v.para(f"{len(reached)} of {total} returns use the edited {edited}, and none of them would move: the largest "
                   f"{LINES[line].split(',')[0].lower()} among them is ", (money(value), {"client": item, "line": line}),
                   f" ({item}), already within the new {money(cap)} cap. " + rest)
        else:
            reach = f"{len(reached)} of {total} returns use the edited {edited}"
            reach += f"; {still} of them would have no line move. " if still else ". "
            v.para(reach + rest)
        return v.answer()

    if decision == "turned down":
        v.head(f"{change.id} was turned down by {approver}. Nothing changed.")
        rows: list[tuple[str, Piece | list[Piece]]] = [("Returns it would have reached", f"{len(reached)} of {total}")]
        v.facts(rows + _gross_rows(report), "Had it been approved")
        v.facts(_edit_rows(change, pack_before, facts_before, cite=True), "What it would have changed")
        return v.answer()

    applied = decision == "approved"
    amend = sum(1 for item in reached if item.route == "amendment")
    waiting = sum(1 for item in reached if item.waiting)
    parts = []
    if amend:
        parts.append(f"{amend} filed {'return' if amend == 1 else 'returns'} {'need' if applied and amend > 1 else 'needs' if applied else 'would need'} amendment review")
    if waiting:
        noun = "" if amend else (" return" if waiting == 1 else " returns")
        parts.append(f"{waiting}{noun} {'wait' if applied and waiting > 1 else 'waits' if applied else 'would wait'} for a document")
    if not parts:
        moved = len(movers)
        parts.append(f"{'the tax changes' if applied else 'the tax would change'} on {moved} {'return' if moved == 1 else 'returns'}" if moved
                     else ("no return's tax changes" if applied else "no return's tax would change"))
    verb = "was applied to" if applied else "reaches"
    v.head(f"{change.id} {verb} {len(reached)} of {total} returns: {' and '.join(parts)}.")
    v.facts(_gross_rows(report))
    v.facts(_edit_rows(change, pack_before, facts_before, cite=True), "What it changed" if decision == "approved" else "What it changes")
    # the change set's own reason, or the sentence a model quoted (checked word for word against the text); never a
    # model's own words
    if change.rationale and (not change.author.startswith("proposed by") or change.rationale.startswith("Quoted from the text:")):
        v.para("Why: " + " ".join(change.rationale.split()))
    if hypothetical:
        v.para("The edit is a what-if, not current law.")
    if change.kind != "rule":
        # what the new document or facts support, set against the return the client filed: the check's own words
        # when it found a conflict, else each Schedule 1-A line that now differs from the return as filed
        conflicts = [[who(item), ": " + item.conflict] for item in reached if item.conflict]
        conflicts += [[who(item), ": ", LINES[line].split(",")[0].lower(), " ", (money(item.filed_values[line]), {"client": item.client_id, "line": line}),
                       " on the return as filed, ", (money(item.after.values[line]), {"client": item.client_id, "line": line}), " now"]
                      for item in reached if item.status == "filed" and item.filed_values and not item.conflict
                      for line in ("sch1a_tips", "sch1a_overtime", "sch1a_senior")
                      if line in item.filed_values and not isinstance(item.after.values[line], Unknown) and item.filed_values[line] != item.after.values[line]]
        v.items(conflicts, "Conflicts with the return as filed")
    v.items(([who(item), " (tax change ", (span(item.tax_range), {"client": item.client_id, "line": "tax"}), " until it arrives)"]
             if item.tax_range else [who(item)] for item in reached if item.waiting), "Document requests")
    v.items(([who(item), " ", (signed(item.filed_delta_tax), {"client": item.client_id, "line": "tax"}), " vs. the filed return"]
             if item.filed_delta_tax is not None
             else [who(item), " ", (span(item.filed_range), {"client": item.client_id, "line": "tax"}), " vs. the filed return, until the document arrives"]
             if item.filed_range else [who(item), ": the amount waits for the document"]
             for item in reached if item.route == "amendment"), "Amendment review")
    if decision == "approved":
        v.para(f"Approved by {approver}.")
    return v.answer()


def _gross_rows(report: Any) -> list[tuple[str, Piece | list[Piece]]]:
    """Totals by direction, never netted across clients: more tax, less tax, waiting on a document, and for filed
    returns owing more or due a refund."""
    rows: list[tuple[str, Piece | list[Piece]]] = []

    def count(n: int) -> str:
        return f"{n} {'return' if n == 1 else 'returns'}, "

    moves, filed = report.tax_moves(), report.filed_moves()
    for label, (n, total_of) in (("More tax", moves["more"]), ("Less tax", moves["less"])):
        if n:
            rows.append((label, [count(n), (signed(total_of), None)]))
    settled = report.settled()
    if settled:
        rows.append(("Settled by this change", [count(len(settled)).removesuffix(", "), ": ",
                                                *[part for i, item in enumerate(settled) for part in ((", " if i else ""), (item.client_id, {"client": item.client_id}))]]))
    waiting = report.unknown_moves()
    if waiting["returns"]:
        whole = waiting["ranged"] == waiting["returns"]
        rows.append(("Waiting for a document", [count(waiting["returns"]), (span((waiting["low"], waiting["high"])), None), " until the documents arrive"]
                     if whole else f"{waiting['returns']} {'return' if waiting['returns'] == 1 else 'returns'}"))
    for label, (n, total_of) in (("Filed returns owing more", filed["more"]), ("Filed returns due a refund", filed["less"])):
        if n:
            rows.append((label, [count(n), (signed(total_of), None), " vs. the filed returns"]))
    priced = [item for item in report.reached if getattr(item, "conflict", "") and getattr(item, "conflict_delta", None) is not None]
    if priced:
        total = sum((item.conflict_delta for item in priced), Decimal(0))
        rows.append(("Conflicts, priced under the Notice", [count(len(priced)), (signed(total), None), " once the Notice's rule applies"]))
    return rows


def rejected_answer(change: ChangeSet, errors: Iterable[Any], pack_version: int, *, stopped: bool = False) -> Answer:
    """The answer for a change that failed validation (errors grouped by the parameter or field they name) or
    that the full recompute stopped."""
    v = _View("rejected")
    errors = list(errors)
    if stopped:
        v.head(f"{change.id} was stopped. Nothing changed.")
        v.para("The full recompute found a change in a return outside its reach, so the change was not applied.")
        v.items(([getattr(error, "message", str(error))] for error in errors), "Details")
    else:
        v.head(f"{change.id} failed validation. Nothing changed.")
        groups: dict[str, list[tuple[str, Piece | list[Piece]]]] = {}
        for error in errors:
            name, message = (error.field, error.message) if hasattr(error, "field") else ("", str(error))
            base, _, tail = name.rpartition(".")
            if name in LABELS:
                groups.setdefault(name, []).append(("Value", message))
            elif tail == "cite" and base in LABELS:
                groups.setdefault(base, []).append(("Citation", message))
            else:
                groups.setdefault(name, []).append(("Problem", message))
        for key, rows in groups.items():
            v.facts(rows, _cap(LABELS.get(key, key.replace("_", " ")) or "The change"))
    v.facts([("Rules", f"version {pack_version}, unchanged"), ("Client facts", "last valid version, unchanged")])
    return v.answer()


TRY = (
    "What if the overtime cap drops to $10,000?",
    "Where does C-017's overtime deduction come from?",
    "Run CS-001: count only the FLSA-required overtime premium",
)


def unsupported_answer(reason: str, can_do: Iterable[str]) -> Answer:
    """The answer for a question the tool did not read: why, what it reads, and questions to try."""
    v = _View("unsupported")
    v.head("This prototype cannot run that question.")
    v.para(_cap(reason) + ".")
    v.para("It reads a change set by ID, a rule with a new value, an employer statement on file, or a question about one client's return.")
    v.items(([(question, {"question": question})] for question in TRY), "Try one of these")
    return v.answer()


RUN_CS001 = "Run CS-001: count only the FLSA-required overtime premium"


def session_answer(topic: str, latest: Mapping[str, tuple[ChangeSet, ReturnImpact]], waiting: Mapping[str, tuple[str, ...]],
                   approved: int) -> Answer:
    """The answer to a question about the session: the filed returns to amend (and to tell about), or the
    returns that wait for a document. latest maps each client to the last approved change that reached it."""
    v = _View("session")
    if topic == "amendments":
        amend = [(cid, change, item) for cid, (change, item) in latest.items() if item.route == "amendment"]
        tell = [(cid, change, item) for cid, (change, item) in latest.items() if item.route == "below_materiality"]
        n = len(amend)
        v.head(f"{n} filed {'return needs' if n == 1 else 'returns need'} an amended return." if n
               else "No filed return needs an amended return in this session.")

        def row(cid: str, change: ChangeSet, item: ReturnImpact) -> list[Piece]:
            if item.filed_delta_tax is None and item.filed_range:
                return [(cid, {"client": cid}), " ", (span(item.filed_range), {"client": cid, "line": "tax"}),
                        f" vs. the filed return, until the document arrives ({change.id})"]
            amount: Piece = ((signed(item.filed_delta_tax), {"client": cid, "line": "tax"}) if item.filed_delta_tax is not None
                             else "amount waits for a document")
            return [(cid, {"client": cid}), " ", amount, f" vs. the filed return ({change.id})"]

        v.items((row(*entry) for entry in amend), "Amendment review")
        v.items((row(*entry) for entry in tell), "Too small to amend: tell the client")
        if amend or tell:
            v.para("Each has a client notice in its note; an amended return follows the reviewer's sign-off.")
    else:
        n = len(waiting)
        v.head(f"{n} {'return waits' if n == 1 else 'returns wait'} for a document." if n else "No return is waiting for a document in this session.")
        v.items(([(cid, {"client": cid}), ": " + "; ".join(needs)] for cid, needs in waiting.items()), "Waiting for")
    if not approved:
        v.para("No change has been approved in this session yet.")
        v.items([[(RUN_CS001, {"question": RUN_CS001})]], "Try")
    return v.answer()


def _value(v: Value) -> str:
    return "unknown" if isinstance(v, Unknown) else money(v)


def client_answer(
    client_id: str,
    line: str,
    direction: str,
    *,
    start: Any,
    now: Any,
    moves: list[tuple[ChangeSet, ReturnImpact]],
    reached_by: list[str],
    reads: list[tuple[str, str, str, str]],
    as_filed: Mapping[str, Value] | None,
    filing_status: str,
    inputs: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    ends: tuple[Any, Any] | None = None,
) -> Answer:
    """The answer for a question about one client's line: the result first, then the values it moved
    between, what moved it, and what it is computed from.

    start and now are the ComputedReturn at the session start and now; moves are the approved changes that
    moved this line, with their impact on the client; reached_by names every approved change that reached
    the client; reads are (kind, name, label, value) for the line's own inputs, the edited ones first;
    inputs are (name, value, line) for the earlier lines it is computed from; ends are the return computed with
    each missing amount at the low and the high end of its range (engine.extremes), when the line waits on one.
    """
    label = LINES[line].split(",")[0].lower()
    first, last = start.values[line], now.values[line]
    link = {"client": client_id, "line": line}
    who: Piece = (client_id, {"client": client_id})
    v = _View("client")
    known = not isinstance(first, Unknown) and not isinstance(last, Unknown)
    actual = ("up" if last > first else "down" if last < first else "") if known else ""
    mfs = filing_status == "mfs" and line.startswith("sch1a")
    if mfs and known and last == 0:
        v.head(who, f"'s {label} is ", (_value(last), link), f" because {client_id} files married filing separately.")
    elif direction and known and actual != direction:
        if actual:
            v.head(who, f"'s {label} did not go {direction}: it went {actual} ", (money(abs(delta(first, last))), link), ", from ",
                   (_value(first), link), " to ", (_value(last), link), ".")
        else:
            v.head(who, f"'s {label} did not go {direction}: it has not changed in this session.")
    elif known and not actual:
        v.head(who, f"'s {label} is ", (_value(last), link), ", the same as at the start of this session.")
    elif known:
        v.head(who, f"'s {label} went {actual} ", (money(abs(delta(first, last))), link), " in this session, from ", (_value(first), link),
               " to ", (_value(last), link), ".")
    elif (span_now := bounds(ends, line)) is not None and isinstance(last, Unknown):
        v.head(who, f"'s {label} is between ", (money(span_now[0]), link), " and ", (money(span_now[1]), link), " until a document arrives.")
    else:
        v.head(who, f"'s {label} is ", (_value(last), link), " until a document arrives." if isinstance(last, Unknown) else ".")
    if mfs:
        v.para("A married taxpayer must file a joint return to claim the tips deduction (IRC 224(f)), the overtime deduction "
               "(IRC 225(e)) or the senior deduction (Schedule 1-A, part V), so none of them applies to a separate return.")

    filed = as_filed.get(line) if as_filed else None
    same_start = filed is not None and not isinstance(first, Unknown) and filed == first
    rows: list[tuple[str, Piece | list[Piece]]] = []
    if actual or not known:
        rows.append(("At the start of this session and as filed" if same_start else "At the start of this session", (_value(first), link)))
        rows.append(("Now", (_value(last), link)))
    elif same_start:
        rows.append(("Now and as filed", (_value(last), link)))
    else:
        rows.append(("Now", (_value(last), link)))
    if filed is not None and not same_start:
        rows.append(("As filed", (money(filed), link)))
        if not isinstance(last, Unknown) and last != filed:
            rows.append(("Now, vs. the filed return", (signed(delta(filed, last)), link)))
    if isinstance(last, Unknown) and (span_now := bounds(ends, line)) is not None:
        rows.append(("Now, until the document arrives", [(money(span_now[0]), link), " to ", (money(span_now[1]), link)]))
        if filed is not None and not isinstance(filed, Unknown):
            rows.append(("Against the return as filed", (span((delta(filed, span_now[0]), delta(filed, span_now[1]))), link)))
    if moves:
        rows.append(("Route", ROUTES[moves[-1][1].route]))
    v.facts(rows)

    for change, item in moves:
        diff = next(d for d in item.diffs if d.line == line)
        v.para(f"{change.id} ({change.title}) moved it from ", (_value(diff.before), link), " to ", (_value(diff.after), link), ".")
        others = [d for d in item.diffs if d.line not in (line, "magi", "tax_formula")]
        v.facts(((LINES[d.line].split(",")[0], [(_value(d.before), {"client": client_id, "line": d.line}), " to ",
                                                (_value(d.after), {"client": client_id, "line": d.line})]) for d in others),
                f"Other lines {change.id} moved on {client_id}")
    if not moves:
        if reached_by:
            v.para(f"Approved change{'s' if len(reached_by) > 1 else ''} {', '.join(reached_by)} reached this return without moving this line.")
        else:
            v.para("No approved change in this session has reached this return.")
    if isinstance(last, Unknown):
        v.items(([need] for need in last.needs), "Waiting for")
    worksheet = now.work.get(line, ())
    if worksheet:
        # the form's own steps, as the engine figured them: the derivation, not just the inputs
        v.facts(((f"Line {ref}: {label}" if ref else label, _sheet_value(value)) for ref, label, value in worksheet), "How it is figured")
    v.facts(((_cap(name), (value, {"client": client_id, "line": source})) for name, value, source in inputs), "Computed from")
    v.facts(((name_label, value) for _, _, name_label, value in reads), "Its own inputs")
    return v.answer()


def _sheet_value(value: Any) -> str:
    if isinstance(value, Unknown):
        return "not on file"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return money(value)


def overtime_definition(pack: RulePack) -> str:
    return OVERTIME[pack.value("overtime.definition")].summary


def _canon(raw: str) -> str:
    whole, _, fraction = raw.lstrip("+-").replace(",", "").partition(".")
    whole, fraction = whole.lstrip("0") or "0", fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def figures_outside(text: str, data: Any) -> list[str]:
    """Figures in the text that appear nowhere in the data (a run's JSON): an invented number shows up here."""
    known: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, bool) or value is None:
            return
        else:
            known.update(_canon(m) for m in re.findall(r"\d[\d,]*(?:\.\d+)?", str(value)))

    walk(data)
    return [f for f in figures(text) if _canon(f) not in known]
