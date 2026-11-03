"""Impact: recompute the book, diff the returns a change reaches, and check the rest did not move."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from time import perf_counter

from .changes import Touched
from .engine import LINES, ComputedReturn, bounds, compute, extremes
from .facts import Book
from .provenance import Unknown, Value
from .router import route
from .rules import RulePack


def compute_book(book: Book, pack: RulePack) -> dict[str, ComputedReturn]:
    return {ret.client_id: compute(ret, pack) for ret in book.returns}


def _covers(touched: str, read: str) -> bool:
    """A touched path reaches a read of the same path or of anything below it (people[0] covers people[0].wages)."""
    return read == touched or read.startswith((f"{touched}.", f"{touched}["))


def find_reached(touched: set[Touched], results: dict[str, ComputedReturn]) -> set[str]:
    """Clients whose recorded reads include a touched path, or a path below it."""
    reached = set()
    for client_id, result in results.items():
        reads = result.all_reads
        for client, path in touched:
            if (client is None or client == client_id) and (path in reads or any(_covers(path, read) for read in reads)):
                reached.add(client_id)
                break
    return reached


def delta(before: Value | None, after: Value | None) -> Decimal | None:
    """after minus before, or None when either side is Unknown or missing. The one place a change is computed."""
    if before is None or after is None or isinstance(before, Unknown) or isinstance(after, Unknown):
        return None
    return after - before


@dataclass(frozen=True)
class LineDiff:
    line: str
    before: Value
    after: Value

    @property
    def delta(self) -> Decimal | None:
        return delta(self.before, self.after)


@dataclass(frozen=True)
class ReturnImpact:
    client_id: str
    status: str
    before: ComputedReturn
    after: ComputedReturn
    diffs: tuple[LineDiff, ...]
    delta_tax: Decimal | None
    filed_delta_tax: Decimal | None
    route: str = ""
    # while a document is outstanding: the least and the most the tax change can be, against the return before
    # the change and against the return as filed (None when the change is known or has no bound)
    tax_range: tuple[Decimal, Decimal] | None = None
    filed_range: tuple[Decimal, Decimal] | None = None
    filed_values: Mapping[str, Value] | None = None  # the lines as filed, for a filed return
    conflict: str = ""  # a new document or fact that contradicts the return as filed, in words
    # the conflict priced under the Notice's rule: the tax change against the return as filed (or as prepared),
    # when the active rules still count all overtime pay; None when it is not priced
    conflict_delta: Decimal | None = None

    @property
    def waiting(self) -> bool:
        """This change left the return waiting on a document: a line it moved is now unknown."""
        return bool(self.needs) and bool(self.diffs)

    @property
    def needs(self) -> tuple[str, ...]:
        return self.after.needs


@dataclass
class ImpactReport:
    reached: list[ReturnImpact] = field(default_factory=list)
    not_reached: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    results: dict[str, ComputedReturn] = field(default_factory=dict)
    ms: float = 0.0  # the whole-book recompute

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.reached:
            out[item.route] = out.get(item.route, 0) + 1
        return out

    def tax_change(self) -> tuple[Decimal | None, int]:
        """The sum of the known changes in tax across reached returns, and how many wait on a document."""
        known = [item.delta_tax for item in self.reached if item.delta_tax is not None]
        return (sum(known, Decimal(0)) if known else None), sum(1 for item in self.reached if item.waiting)

    def settled(self) -> list[ReturnImpact]:
        """Reached returns whose tax was unknown before the change and is known after it: a document arrived."""
        return [item for item in self.reached if item.delta_tax is None and not item.needs]

    def tax_moves(self) -> dict[str, tuple[int, Decimal]]:
        """The known changes in tax split by direction: (returns, sum) for more tax and for less tax."""
        up = [item.delta_tax for item in self.reached if item.delta_tax is not None and item.delta_tax > 0]
        down = [item.delta_tax for item in self.reached if item.delta_tax is not None and item.delta_tax < 0]
        return {"more": (len(up), sum(up, Decimal(0))), "less": (len(down), sum(down, Decimal(0)))}

    def moved_filed(self) -> list[ReturnImpact]:
        """Filed returns whose tax this change moves or settles. A filed return the change reaches without moving
        it may still differ from the return as filed because of an earlier change; that is not this change's effect."""
        settled = {item.client_id for item in self.settled()}
        return [item for item in self.reached if item.status == "filed" and item.filed_delta_tax is not None
                and ((item.delta_tax is not None and item.delta_tax != 0) or item.client_id in settled)]

    def filed_moves(self) -> dict[str, tuple[int, Decimal]]:
        """The changes against the returns as filed, split into owing more and a refund, over the filed returns
        this change moves or settles."""
        filed = [item.filed_delta_tax for item in self.moved_filed() if item.filed_delta_tax is not None]
        up, down = [d for d in filed if d > 0], [d for d in filed if d < 0]
        return {"more": (len(up), sum(up, Decimal(0))), "less": (len(down), sum(down, Decimal(0)))}

    def unknown_moves(self) -> dict[str, object]:
        """The reached returns whose tax change waits on a document, and the sum of their ranges where known."""
        waiting = [item for item in self.reached if item.waiting]
        ranged = [item.tax_range for item in waiting if item.tax_range is not None]
        low = sum((r[0] for r in ranged), Decimal(0)) if ranged else None
        high = sum((r[1] for r in ranged), Decimal(0)) if ranged else None
        return {"returns": len(waiting), "ranged": len(ranged), "low": low, "high": high}

    def filed_change(self) -> Decimal | None:
        """The sum of the changes against the return as filed, over the filed returns this change moves or settles."""
        known = [item.filed_delta_tax for item in self.moved_filed() if item.filed_delta_tax is not None]
        return sum(known, Decimal(0)) if known else None


_tax_delta = delta


def analyze(
    touched: set[Touched],
    before: dict[str, ComputedReturn],
    as_filed: Mapping[str, Mapping[str, Value]],
    book: Book,
    pack: RulePack,
) -> ImpactReport:
    """Recompute the whole book once and diff the returns the change reaches.

    The recorded reads in `before` pick out the reached returns. Each reached return is diffed line by line
    against its result before the change, and a filed one also gets its change in tax against the amounts it
    was filed with (`as_filed`). Any return outside the reached list whose values moved is listed as
    unexpected. Reached returns come back unrouted; the route lane routes them once Engine B has finished.
    """
    reached_ids = find_reached(touched, before)
    started = perf_counter()
    after = compute_book(book, pack)
    report = ImpactReport(results=after, ms=(perf_counter() - started) * 1000)
    returns = {ret.client_id: ret for ret in book.returns}
    for client_id in sorted(before):
        old, new = before[client_id], after[client_id]
        if client_id not in reached_ids:
            report.not_reached.append(client_id)
            if old.values != new.values:
                report.unexpected.append(client_id)
            continue
        diffs = tuple(LineDiff(line, old.values[line], new.values[line]) for line in LINES if old.values[line] != new.values[line])
        filed = as_filed.get(client_id)
        tax_range = filed_range = None
        if isinstance(new.values["tax"], Unknown):
            ends = bounds(extremes(returns[client_id], pack), "tax")
            if ends and not isinstance(old.values["tax"], Unknown):
                tax_range = (_tax_delta(old.values["tax"], ends[0]), _tax_delta(old.values["tax"], ends[1]))
            if ends and filed and not isinstance(filed["tax"], Unknown):
                filed_range = (_tax_delta(filed["tax"], ends[0]), _tax_delta(filed["tax"], ends[1]))
        report.reached.append(
            ReturnImpact(
                client_id=client_id,
                status=new.status,
                before=old,
                after=new,
                diffs=diffs,
                delta_tax=_tax_delta(old.values["tax"], new.values["tax"]),
                filed_delta_tax=_tax_delta(filed["tax"], new.values["tax"]) if filed else None,
                tax_range=tax_range,  # type: ignore[arg-type]
                filed_range=filed_range,  # type: ignore[arg-type]
                filed_values=filed,
            )
        )
    return report


def route_all(report: ImpactReport, materiality: Decimal, differs: set[str]) -> None:
    """Route every reached return. differs holds the clients where the two engines disagree; a return whose
    `conflict` is set routes on it."""
    report.reached = [
        replace(
            item,
            route=route(
                item.status,
                bool(item.diffs),
                bool(item.needs),
                item.filed_delta_tax,
                materiality,
                engines_differ=item.client_id in differs,
                conflict=bool(item.conflict),
                conflict_delta=item.conflict_delta,
            ),
        )
        for item in report.reached
    ]
