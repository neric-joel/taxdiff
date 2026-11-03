"""The session a request carries, rebuilt on the server for every request.

The server keeps no state between requests. The browser sends the change sets it has approved, in order,
and the server replays them from rule pack v1 and the shipped book: Engine A is deterministic and the book
is 40 synthetic returns, so a replay takes milliseconds and gives the same state every time. Every replayed
change is validated again; a document change may name only a statement shipped in data/documents/.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..changes import ChangeSet
from ..facts import load_book, normal_path, parse_path
from ..paths import BOOK, DOCUMENTS, ROOT, RULES
from ..rules import load_pack
from ..workflow import ParityMode, Run, State, approve_run, initial_state, run_change
from . import signing

PARITY_MODE: ParityMode = os.environ.get("TAXDIFF_PARITY", "cache")  # type: ignore[assignment]
if PARITY_MODE not in ("cache", "live", "api", "off"):
    PARITY_MODE = "cache"
MAX_HISTORY = 25


class Approved(BaseModel):
    """One change set the reviewer approved earlier in this browser session, with the server's receipt."""

    model_config = ConfigDict(extra="forbid")

    change: ChangeSet
    approver: str = Field(min_length=1, max_length=80)
    materiality: Decimal = Field(default=Decimal(50), ge=0, le=1_000_000, decimal_places=2)
    receipt: str = Field(default="", max_length=64)


def tip(history: list[Approved]) -> str:
    """The receipt of the last approved change: what a preview or the next receipt is bound to."""
    return history[-1].receipt if history else ""


def receipt_for(previous: str, change: ChangeSet, approver: str, materiality: Decimal, digest: str) -> str:
    return signing.sign("approval", prev=previous, change=change.model_dump(mode="json"), approver=approver,
                        materiality=str(materiality), digest=digest)


def statements_on_file() -> set[str]:
    """The shipped statements a document change may name, as repository-relative paths."""
    return {p.relative_to(ROOT).as_posix() for p in DOCUMENTS.iterdir() if p.suffix.lower() in (".txt", ".pdf")}


def check_change(change: ChangeSet) -> list[str]:
    """Problems that keep a change set from being run here at all (field validation comes later)."""
    problems = []
    if change.kind == "document":
        if change.document not in statements_on_file():
            problems.append(f"document: {change.document!r} is not a statement on file in data/documents/")
        if not change.client_id:
            problems.append("client_id: a document change names its client")
    elif change.document is not None:
        problems.append("document: only a document change names a document")
    return problems


@lru_cache(maxsize=1)
def base() -> State:
    """The session start: rule pack v1 and the shipped book, computed once per server process."""
    return initial_state(load_pack(RULES), load_book(BOOK), PARITY_MODE)


@dataclass
class Sources:
    """Which change last set each rule parameter and each client fact, for the provenance chain."""

    rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def record(self, run: Run) -> None:
        change = run.change
        if change.kind == "rule":
            for edit in change.rule_edits:
                self.rules[edit.path] = {"change": change.id, "title": change.title, "cite": edit.cite}
            return
        fields = {"ot_premium_reported": "premium", "ot_flsa_hours": "flsa_hours", "ot_regular_rate": "regular_rate"}
        for edit in change.fact_edits:
            path = normal_path(edit.path)
            entry: dict[str, Any] = {"change": change.id, "title": change.title}
            if change.kind == "document":
                entry["document"] = change.document
                entry["field"] = fields.get(str(parse_path(path)[-1]), "")
            self.facts[(edit.client_id, path)] = entry


@dataclass
class Session:
    initial: State
    state: State
    runs: list[Run]  # the approved changes, replayed in order
    ms: float  # how long the replay took
    sources: Sources
    audit_start: int  # audit events before this index came from the replay


class HistoryError(ValueError):
    def __init__(self, index: int, change_id: str, reason: str) -> None:
        super().__init__(f"approved change {index + 1} ({change_id}) no longer applies: {reason}")
        self.index, self.change_id, self.reason = index, change_id, reason


def replay(history: list[Approved], law_mode: Literal["cache", "live"] = "cache") -> Session:
    """Apply the approved changes in order from the session start. Raises HistoryError if one fails.

    Each change is previewed again and its receipt checked against that preview and the receipt before
    it; only then is it approved. A change this server did not approve, or approved in another order or
    against other data, stops the replay.
    """
    started = perf_counter()
    initial = base()
    state, runs, sources, previous = initial, [], Sources(), ""
    for index, item in enumerate(history):
        problems = check_change(item.change)
        if problems:
            raise HistoryError(index, item.change.id, "; ".join(problems))
        preview = run_change(state, item.change, materiality=item.materiality, parity_mode=PARITY_MODE, law_mode=law_mode)
        if preview.pending is None:
            reasons = "; ".join(str(e) for e in (preview.validation.errors if preview.validation else [])) or "it did not pass its checks"
            raise HistoryError(index, item.change.id, reasons)
        digest = signing.preview_digest(preview, item.materiality)
        if not signing.verify(item.receipt, "approval", prev=previous, change=item.change.model_dump(mode="json"),
                              approver=item.approver, materiality=str(item.materiality), digest=digest):
            raise HistoryError(index, item.change.id, "its approval receipt does not match: it was not approved on this server, "
                                                      "or the server's data changed since; reset the session")
        run = approve_run(preview, item.approver)
        if not run.applied or run.state is None:
            raise HistoryError(index, item.change.id, "the approval did not apply")
        sources.record(run)
        runs.append(run)
        state, previous = run.state, item.receipt
    return Session(initial, state, runs, (perf_counter() - started) * 1000, sources, len(state.audit))


def next_change_id(history: list[Approved], used: int = 0) -> str:
    """The id for a change read from a question: one more than the highest written id so far (CS-100 first),
    and past the ids this session has already shown, which the browser counts in `used`."""
    written = [int(item.change.id[3:]) for item in history if int(item.change.id[3:]) >= 100]
    return f"CS-{min(max(max(written, default=99) + 1, 100 + used), 999):03d}"
