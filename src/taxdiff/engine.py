"""Deterministic TY2025 Form 1040 computation with line-level provenance (Engine A)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .facts import ClientReturn
from .provenance import Tracker, Unknown, Value, total
from .provisions import OVERTIME, cents, usd
from .rules import RulePack
from .tax import bracket_tax, form_tax

LINES: dict[str, str] = {
    "wages": "Wages, W-2 box 1",
    "agi": "Adjusted gross income, line 11b",
    "magi": "Modified AGI, Schedule 1-A line 3",
    "standard_deduction": "Standard deduction, line 12e",
    "sch1a_tips": "Tips deduction, Schedule 1-A line 13",
    "sch1a_overtime": "Overtime deduction, Schedule 1-A line 21",
    "sch1a_senior": "Senior deduction, Schedule 1-A line 37",
    "sch1a_total": "Schedule 1-A deductions, line 13b",
    "taxable_income": "Taxable income, line 15",
    "tax_formula": "Tax by rate schedule (what the cross-check compares)",
    "tax": "Tax, line 16",
}

# Where each line sits on the 2025 forms ("1-A 21" is Schedule 1-A line 21); the cross-check's tax line has none.
LINE_NUMBERS: dict[str, str] = {
    "wages": "1a",
    "agi": "11b",
    "magi": "1-A 3",
    "standard_deduction": "12e",
    "sch1a_tips": "1-A 13",
    "sch1a_overtime": "1-A 21",
    "sch1a_senior": "1-A 37",
    "sch1a_total": "13b",
    "taxable_income": "15",
    "tax_formula": "",
    "tax": "16",
}

ZERO = Decimal(0)


@dataclass(frozen=True)
class ComputedReturn:
    client_id: str
    status: str
    filing_status: str
    pack_version: int
    facts_version: int
    values: Mapping[str, Value]
    reads: Mapping[str, frozenset[str]]
    uses: Mapping[str, frozenset[str]] = field(default_factory=dict)  # earlier lines each line used
    # each line's worksheet steps (form line, label, value), as the engine figured them; not compared
    work: Mapping[str, tuple[tuple[str, str, Any], ...]] = field(default_factory=dict, compare=False)

    @property
    def all_reads(self) -> frozenset[str]:
        return frozenset().union(*self.reads.values())

    @property
    def needs(self) -> tuple[str, ...]:
        value = self.values["tax"]
        return value.needs if isinstance(value, Unknown) else ()

    def changed_lines(self, other: ComputedReturn) -> list[str]:
        return [name for name in LINES if self.values[name] != other.values[name]]


def _reduction(tr: Tracker, prefix: str, magi: Decimal, filing_status: str, refs: tuple[str, ...] = ()) -> Decimal:
    """Phase-out reduction counted in whole steps of MAGI over the start. With refs (the worksheet lines for
    MAGI, the start, the excess, the steps and the reduction), each step is recorded as it is figured."""
    start = tr.rule(f"{prefix}.phaseout.start.{'mfj' if filing_status == 'mfj' else 'other'}")
    step = tr.rule(f"{prefix}.phaseout.step")
    per_step = tr.rule(f"{prefix}.phaseout.reduction_per_step")
    excess = max(ZERO, magi - start)
    steps = excess // step
    reduction = steps * per_step
    if refs:
        for ref, label, value in zip(refs, ("Modified AGI", "Phase-out start", "Modified AGI over the start, not below zero",
                                            f"That amount divided by {usd(step)}, rounded down", f"Times {usd(per_step)}"),
                                     (magi, start, excess, int(steps), reduction), strict=True):
            tr.show(ref, label, value)
    return reduction


def compute(ret: ClientReturn, pack: RulePack, missing: str | None = None) -> ComputedReturn:
    """Compute one return. missing is None, or "low" or "high" to take each missing amount at that end of its
    range (see extremes)."""
    tr = Tracker(pack, missing)
    people = ret.people

    with tr.line("wages"):
        tr.fact("people", len(people))
        tr.set("wages", sum((tr.fact(f"people[{i}].wages", p.wages) for i, p in enumerate(people)), ZERO))
    with tr.line("agi"):
        tr.set("agi", tr.use("wages"))
    with tr.line("magi"):
        tr.set("magi", tr.use("agi"))

    with tr.line("standard_deduction"):
        status = tr.fact("filing_status", ret.filing_status)
        amount = tr.rule(f"standard_deduction.{status}")
        cutoff = tr.rule("age65.born_before")
        older = sum(1 for i, p in enumerate(people) if tr.fact(f"people[{i}].birth_date", p.birth_date) < cutoff)
        if older:
            married = status in ("mfj", "mfs")
            amount += older * tr.rule(f"standard_deduction.age65.{'married' if married else 'unmarried'}")
        tr.set("standard_deduction", amount)

    with tr.line("sch1a_tips"):
        tips: list[Value] = []
        for i, p in enumerate(people):
            amount = tr.fact(f"people[{i}].tips_qualified", p.tips_qualified)
            if not amount:
                continue
            if not tr.fact(f"people[{i}].valid_ssn", p.valid_ssn):
                continue
            listed = tr.fact(f"people[{i}].tips_occupation_listed", p.tips_occupation_listed)
            if listed is None and tr.missing:
                tips.append(amount if tr.missing == "high" else ZERO)
            elif listed is None:
                tips.append(Unknown((f"whether the {p.role}'s tipped occupation is on the IRS list",)))
            elif listed:
                tips.append(amount)
        value: Value = ZERO
        if tips and (status := tr.fact("filing_status", ret.filing_status)) != "mfs":
            cap = tr.rule("tips.cap")
            qualified = total(*tips)
            tr.show("", "Qualified tips", qualified)
            tr.show("6", f"Smaller of the qualified tips or {usd(cap)}", qualified if isinstance(qualified, Unknown) else min(qualified, cap))
            reduction = _reduction(tr, "tips", tr.use("magi"), status, ("", "9", "10", "11", "12"))
            if reduction >= cap:
                value = ZERO  # the phase-out removes any amount the missing fact could add
            elif isinstance(qualified, Unknown):
                value = qualified
            else:
                value = max(ZERO, min(qualified, cap) - reduction)
            tr.show("13", "Tips deduction: line 6 minus line 12, not below zero", value)
        elif tips:
            tr.show("13", "Married filing separately: no tips deduction (IRC 224(f))", ZERO)
        tr.set("sch1a_tips", value)

    with tr.line("sch1a_overtime"):
        premiums: list[Value] = []
        for i, p in enumerate(people):
            evidence = (
                tr.fact(f"people[{i}].ot_box14", p.ot_box14),
                tr.fact(f"people[{i}].ot_premium_reported", p.ot_premium_reported),
                tr.fact(f"people[{i}].ot_premium_paid", p.ot_premium_paid),
                tr.fact(f"people[{i}].ot_flsa_hours", p.ot_flsa_hours),
            )
            if all(item is None for item in evidence):
                continue
            if not tr.fact(f"people[{i}].valid_ssn", p.valid_ssn):
                continue
            provision = OVERTIME[tr.rule("overtime.definition")]
            tr.note(f"code:overtime.{provision.name}@{provision.version}")
            premiums.append(provision.compute(tr, i, p))
        value = ZERO
        if premiums and (status := tr.fact("filing_status", ret.filing_status)) != "mfs":
            cap = tr.rule("overtime.cap.mfj" if status == "mfj" else "overtime.cap.other")
            qualified = total(*premiums)
            tr.show("14c", "Qualified overtime compensation", qualified)
            tr.show("15", f"Smaller of line 14c or {usd(cap)}", qualified if isinstance(qualified, Unknown) else min(qualified, cap))
            reduction = _reduction(tr, "overtime", tr.use("magi"), status, ("16", "17", "18", "19", "20"))
            if reduction >= cap:
                value = ZERO  # the phase-out removes any premium the missing fact could add
            elif isinstance(qualified, Unknown):
                value = qualified
            else:
                value = max(ZERO, min(qualified, cap) - reduction)
            tr.show("21", "Overtime deduction: line 15 minus line 20, not below zero", value)
        elif premiums:
            tr.show("21", "Married filing separately: no overtime deduction (IRC 225(e))", ZERO)
        tr.set("sch1a_overtime", value)

    with tr.line("sch1a_senior"):
        cutoff = tr.rule("senior.born_before")
        eligible = [
            i
            for i, p in enumerate(people)
            if tr.fact(f"people[{i}].birth_date", p.birth_date) < cutoff
            and tr.fact(f"people[{i}].valid_ssn", p.valid_ssn)
        ]
        value = ZERO
        if eligible and (status := tr.fact("filing_status", ret.filing_status)) != "mfs":
            start = tr.rule(f"senior.phaseout.start.{'mfj' if status == 'mfj' else 'other'}")
            magi = tr.use("magi")
            excess = max(ZERO, magi - start)
            amount, rate = tr.rule("senior.amount"), tr.rule("senior.phaseout.rate")
            each = max(ZERO, amount - rate * excess)
            value = cents(each) * len(eligible)
            tr.show("", "Modified AGI", magi)
            tr.show("32", "Phase-out start", start)
            tr.show("33", "Modified AGI over the start, not below zero", excess)
            tr.show("34", f"Line 33 times {rate * 100:g}%", cents(rate * excess))
            tr.show("35", f"{usd(amount)} minus line 34, not below zero, for each eligible person", cents(each))
            tr.show("37", f"Senior deduction for {len(eligible)} eligible {'person' if len(eligible) == 1 else 'people'}", value)
        elif eligible:
            tr.show("37", "Married filing separately: no senior deduction (Schedule 1-A part V)", ZERO)
        tr.set("sch1a_senior", value)

    with tr.line("sch1a_total"):
        tr.set("sch1a_total", total(tr.use("sch1a_tips"), tr.use("sch1a_overtime"), tr.use("sch1a_senior")))

    with tr.line("taxable_income"):
        agi, deduction, sch1a = tr.use("agi"), tr.use("standard_deduction"), tr.use("sch1a_total")
        if isinstance(sch1a, Unknown):
            tr.set("taxable_income", sch1a)
        else:
            tr.set("taxable_income", max(ZERO, agi - deduction - sch1a))

    with tr.line("tax_formula"):
        taxable = tr.use("taxable_income")
        brackets = tr.rule(f"brackets.{tr.fact('filing_status', ret.filing_status)}")
        tr.set("tax_formula", taxable if isinstance(taxable, Unknown) else bracket_tax(brackets, taxable))

    with tr.line("tax"):
        taxable = tr.use("taxable_income")
        brackets = tr.rule(f"brackets.{tr.fact('filing_status', ret.filing_status)}")
        below = tr.rule("tax_table.below")
        tr.set("tax", taxable if isinstance(taxable, Unknown) else form_tax(brackets, taxable, below))

    return ComputedReturn(
        client_id=ret.client_id,
        status=ret.status,
        filing_status=ret.filing_status,
        pack_version=pack.version,
        facts_version=ret.facts_version,
        values=dict(tr.values),
        reads={name: frozenset(paths) for name, paths in tr.reads.items()},
        uses={name: frozenset(lines) for name, lines in tr.uses.items()},
        work={name: tuple(steps) for name, steps in tr.work.items()},
    )


def extremes(ret: ClientReturn, pack: RulePack) -> tuple[ComputedReturn, ComputedReturn] | None:
    """The return computed with every missing amount at the low end of its range, then at the high end, or None
    when a missing amount has no known range. A missing amount only adds to a deduction, and the tax never rises
    as a deduction grows, so the two results bound each line until the document arrives. The bounds are shown
    beside the missing fact; they are never filed or used as the line's value."""
    low, high = compute(ret, pack, "low"), compute(ret, pack, "high")
    if isinstance(low.values["tax"], Unknown) or isinstance(high.values["tax"], Unknown):
        return None
    return low, high


def bounds(ends: tuple[ComputedReturn, ComputedReturn] | None, line: str) -> tuple[Decimal, Decimal] | None:
    """The smaller and the larger value of a line across the two extremes."""
    if ends is None:
        return None
    a, b = ends[0].values[line], ends[1].values[line]
    if isinstance(a, Unknown) or isinstance(b, Unknown):
        return None
    return (a, b) if a <= b else (b, a)
