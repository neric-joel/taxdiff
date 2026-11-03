"""Named, versioned definitions that a rule pack selects.

Parameters (caps, thresholds) are data in the rule pack. Definitions (what counts as qualified overtime)
are code: each has a name the pack refers to and a version the engine records as a read, so changing either
one reaches every return that used it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from .facts import Person
from .provenance import Tracker, Unknown, Value


def cents(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd(amount: Decimal) -> str:
    """An amount in a worksheet label: whole dollars without cents."""
    return f"${amount:,.2f}".removesuffix(".00")


def _line(i: int) -> str:
    return f"14{'ab'[i] if i < 2 else ''}"  # Schedule 1-A line 14a for the taxpayer, 14b for the spouse


@dataclass(frozen=True)
class Provision:
    name: str
    version: str
    summary: str
    compute: Callable[[Tracker, int, Person], Value]


def _box14_total(tr: Tracker, i: int, person: Person) -> Value:
    amount = tr.fact(f"people[{i}].ot_box14", person.ot_box14)
    value = amount if amount is not None else Decimal(0)
    tr.show(_line(i), f"Overtime pay in W-2 box 14, all of it (the {person.role}; release 1 reading)", value)
    return value


def _flsa_premium(tr: Tracker, i: int, person: Person) -> Value:
    who = person.role
    reported = tr.fact(f"people[{i}].ot_premium_reported", person.ot_premium_reported)
    if reported is not None:
        tr.show(_line(i), f"FLSA premium the employer reported for the {who} (Notice 2025-69 method A)", reported)
        return reported
    paid = tr.fact(f"people[{i}].ot_premium_paid", person.ot_premium_paid)
    if paid is not None:
        multiple = tr.fact(f"people[{i}].ot_multiple", person.ot_multiple)
        if multiple == Decimal("1.5"):
            # at time and a half, the premium paid above the regular rate is the FLSA premium itself
            tr.show(_line(i), f"Method A (Notice 2025-69): {usd(paid)} of overtime premium paid at 1.5x for the {who}", paid)
            return paid
        if multiple is not None and multiple > Decimal("1.5"):
            share = Fraction(Decimal("0.5") / (multiple - 1)).limit_denominator(12)
            value = cents(paid * Decimal(share.numerator) / Decimal(share.denominator))
            tr.show(_line(i), f"Method C (Notice 2025-69): {usd(paid)} of overtime premium paid at {multiple:g}x for the {who}, "
                              f"times {share.numerator}/{share.denominator}", value)
            return value
    basis = tr.fact(f"people[{i}].ot_basis", person.ot_basis)
    if basis == "state_or_contract":
        tr.show(_line(i), f"Overtime required only by state law or a contract, not by the FLSA, for the {who}: not qualified (Notice 2025-69)", Decimal(0))
        return Decimal(0)
    if basis == "flsa":
        pay = tr.fact(f"people[{i}].ot_box14", person.ot_box14)
        multiple = tr.fact(f"people[{i}].ot_multiple", person.ot_multiple)
        if pay is not None and multiple == Decimal("1.5"):
            value = cents(pay / 3)
            tr.show(_line(i), f"Method B (Notice 2025-69): {usd(pay)} of 1.5x overtime pay for the {who}, divided by 3", value)
            return value
        if pay is not None and multiple == Decimal("2"):
            value = cents(pay / 4)
            tr.show(_line(i), f"Method D (Notice 2025-69): {usd(pay)} of 2x overtime pay for the {who}, divided by 4", value)
            return value
    hours = tr.fact(f"people[{i}].ot_flsa_hours", person.ot_flsa_hours)
    rate = tr.fact(f"people[{i}].ot_regular_rate", person.ot_regular_rate)
    if hours is not None and rate is not None:
        value = cents(Decimal("0.5") * rate * hours)
        tr.show(_line(i), f"Method F (Notice 2025-69): {hours} FLSA overtime hours x {usd(rate)} regular rate x 1/2, for the {who}", value)
        return value
    if tr.missing:
        # Bounds, never a value to file: none of the box 14 pay is FLSA overtime, or all of it is, at the share
        # the notice gives for its pay multiple. Without the multiple there is no bound.
        pay = tr.fact(f"people[{i}].ot_box14", person.ot_box14)
        multiple = tr.fact(f"people[{i}].ot_multiple", person.ot_multiple)
        if pay is not None and multiple in (Decimal("1.5"), Decimal("2")):
            return Decimal(0) if tr.missing == "low" else cents(pay / (3 if multiple == Decimal("1.5") else 4))
    missing = Unknown((f"FLSA overtime premium for the {who}: pay stubs or an employer statement",))
    tr.show(_line(i), f"FLSA premium for the {who}: not on file (needs pay stubs or an employer statement)", missing)
    return missing


OVERTIME: dict[str, Provision] = {
    "box14_total": Provision(
        "box14_total",
        "1",
        "All overtime pay in W-2 box 14 counts as qualified overtime",
        _box14_total,
    ),
    "flsa_premium": Provision(
        "flsa_premium",
        "1",
        "Only the FLSA-required premium counts (IRS Notice 2025-69): a reported premium; one third of "
        "1.5x pay or one fourth of 2x pay for FLSA overtime hours; or half the regular rate times FLSA "
        "overtime hours. Overtime required only by state law or a contract counts as zero",
        _flsa_premium,
    ),
}

DEFINITIONS: dict[str, dict[str, Provision]] = {"overtime": OVERTIME}
