"""Provenance: what each computed line read.

A line records every rule parameter, fact (including facts that were empty) and provision version it
read, plus the reads of every earlier line it used. A change can only alter a return whose recorded
reads include the changed path, which is what the impact list relies on.

The rule for engine code: read every input through Tracker.fact or Tracker.rule. A value read any other
way is invisible to the impact list, and only the full recompute would catch a change to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .rules import RulePack


@dataclass(frozen=True)
class Unknown:
    """A value that cannot be computed until a missing fact arrives."""

    needs: tuple[str, ...]

    def __str__(self) -> str:
        return "Unknown"


Value = Decimal | Unknown


def total(*values: Value) -> Value:
    """Sum values; any Unknown makes the sum Unknown and carries every missing fact."""
    needs = [need for value in values if isinstance(value, Unknown) for need in value.needs]
    if needs:
        return Unknown(tuple(dict.fromkeys(needs)))
    return sum(values, Decimal(0))  # type: ignore[arg-type]


class Tracker:
    """Collects reads per line while one return is computed."""

    def __init__(self, pack: RulePack, missing: str | None = None) -> None:
        self._pack = pack
        self.missing = missing  # "low" or "high": take each missing amount at that end of its range (engine.extremes)
        self._open: list[str] = []
        self.reads: dict[str, set[str]] = {}
        self.uses: dict[str, set[str]] = {}  # the earlier lines each line used, for showing the chain
        self.values: dict[str, Value] = {}
        self.work: dict[str, list[tuple[str, str, Any]]] = {}  # each line's worksheet steps: (form line, label, value)

    @contextmanager
    def line(self, name: str) -> Iterator[None]:
        self.reads.setdefault(name, set())
        self.uses.setdefault(name, set())
        self._open.append(name)
        try:
            yield
        finally:
            self._open.pop()

    def show(self, ref: str, label: str, value: Any) -> None:
        """Record one worksheet step of the line being computed: the form line it fills, what it is, its value.
        Steps record the computation as it happens; they are never read back into it."""
        if self._open:
            self.work.setdefault(self._open[-1], []).append((ref, label, value))

    def note(self, path: str) -> None:
        for name in self._open:
            self.reads[name].add(path)

    def rule(self, key: str) -> Any:
        self.note(f"rule:{key}")
        return self._pack.value(key)

    def fact(self, path: str, value: Any) -> Any:
        self.note(f"fact:{path}")
        return value

    def use(self, line: str) -> Value:
        for name in self._open:
            self.reads[name] |= self.reads[line]
            self.uses[name].add(line)
        return self.values[line]

    def set(self, line: str, value: Value) -> None:
        self.values[line] = value
