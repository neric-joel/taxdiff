"""Rule packs: tax parameters for one tax year, versioned, with a citation on every value."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .provisions import DEFINITIONS

# Every parameter the engine reads, and the kind of value it must hold.
SCHEMA: dict[str, str] = {
    "standard_deduction.single": "money",
    "standard_deduction.mfj": "money",
    "standard_deduction.mfs": "money",
    "standard_deduction.hoh": "money",
    "standard_deduction.age65.married": "money",
    "standard_deduction.age65.unmarried": "money",
    "age65.born_before": "date",
    "overtime.definition": "definition:overtime",
    "overtime.cap.other": "money",
    "overtime.cap.mfj": "money",
    "overtime.phaseout.start.other": "money",
    "overtime.phaseout.start.mfj": "money",
    "overtime.phaseout.step": "positive",
    "overtime.phaseout.reduction_per_step": "money",
    "tips.cap": "money",
    "tips.phaseout.start.other": "money",
    "tips.phaseout.start.mfj": "money",
    "tips.phaseout.step": "positive",
    "tips.phaseout.reduction_per_step": "money",
    "senior.amount": "money",
    "senior.born_before": "date",
    "senior.phaseout.start.other": "money",
    "senior.phaseout.start.mfj": "money",
    "senior.phaseout.rate": "rate",
    "brackets.single": "brackets",
    "brackets.mfj": "brackets",
    "brackets.mfs": "brackets",
    "brackets.hoh": "brackets",
    "tax_table.below": "positive",
}

# What each parameter is, in words, for answers and the provenance chain ("other" is every status but MFJ).
LABELS: dict[str, str] = {
    "standard_deduction.single": "standard deduction, single",
    "standard_deduction.mfj": "standard deduction, married filing jointly",
    "standard_deduction.mfs": "standard deduction, married filing separately",
    "standard_deduction.hoh": "standard deduction, head of household",
    "standard_deduction.age65.married": "added standard deduction at 65, married",
    "standard_deduction.age65.unmarried": "added standard deduction at 65, unmarried",
    "age65.born_before": "counts as 65 if born before",
    "overtime.definition": "qualified overtime definition",
    "overtime.cap.other": "overtime cap for single and head of household filers",
    "overtime.cap.mfj": "overtime cap for joint filers",
    "overtime.phaseout.start.other": "overtime phase-out start for single and head of household filers",
    "overtime.phaseout.start.mfj": "overtime phase-out start for joint filers",
    "overtime.phaseout.step": "overtime phase-out step of MAGI",
    "overtime.phaseout.reduction_per_step": "overtime reduction per step",
    "tips.cap": "tips cap",
    "tips.phaseout.start.other": "tips phase-out start for single and head of household filers",
    "tips.phaseout.start.mfj": "tips phase-out start for joint filers",
    "tips.phaseout.step": "tips phase-out step of MAGI",
    "tips.phaseout.reduction_per_step": "tips reduction per step",
    "senior.amount": "senior deduction per eligible person",
    "senior.born_before": "senior deduction if born before",
    "senior.phaseout.start.other": "senior phase-out start for single and head of household filers",
    "senior.phaseout.start.mfj": "senior phase-out start for joint filers",
    "senior.phaseout.rate": "senior phase-out rate",
    "brackets.single": "rate schedule, single",
    "brackets.mfj": "rate schedule, married filing jointly",
    "brackets.mfs": "rate schedule, married filing separately",
    "brackets.hoh": "rate schedule, head of household",
    "tax_table.below": "Tax Table used below this taxable income",
}

# Plain words for parameter values that are names, not numbers.
VALUE_WORDS: dict[str, dict[str, str]] = {
    "overtime.definition": {"box14_total": "all overtime pay (W-2 box 14)", "flsa_premium": "only the FLSA premium (the extra half)"},
}

# The parameter a filing-status variant pairs with, so an answer can say which one stays as it was.
PAIRS: dict[str, str] = {}
for _key in ("overtime.cap", "overtime.phaseout.start", "tips.phaseout.start", "senior.phaseout.start"):
    PAIRS[f"{_key}.other"], PAIRS[f"{_key}.mfj"] = f"{_key}.mfj", f"{_key}.other"


def value_words(key: str, value: Any) -> str:
    """A name-valued parameter in plain words ("FLSA premium only"); other values as written."""
    return VALUE_WORDS.get(key, {}).get(str(value), str(value))


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


class PackError(ValueError):
    def __init__(self, errors: list[FieldError]) -> None:
        super().__init__("; ".join(str(error) for error in errors))
        self.errors = errors


@dataclass(frozen=True)
class Parameter:
    key: str
    value: Any
    cite: str
    url: str | None = None


@dataclass(frozen=True)
class RulePack:
    name: str
    tax_year: int
    version: int
    parameters: dict[str, Parameter]
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def label(self) -> str:
        return f"pack v{self.version}"

    def value(self, key: str) -> Any:
        return self.parameters[key].value

    def cite(self, key: str) -> str:
        return self.parameters[key].cite

    def url(self, key: str) -> str | None:
        return self.parameters[key].url


MAX_ABS = Decimal("1e12")  # no rule or fact value in this domain comes near a trillion
MAX_PLACES = 6


def bounded(value: Decimal) -> Decimal:
    """The value if its size and precision are plausible, else ValueError. Checked before any arithmetic
    or formatting, so a value like 1e1000000000 is refused without expanding its digits."""
    if not value.is_finite():
        raise ValueError("must be a finite number")
    if value != 0 and value.adjusted() >= MAX_ABS.adjusted():
        raise ValueError("must be under 1,000,000,000,000")
    if value.as_tuple().exponent < -MAX_PLACES and value != value.quantize(Decimal(1).scaleb(-MAX_PLACES)):  # type: ignore[operator]
        raise ValueError(f"can have at most {MAX_PLACES} decimal places")
    return value


def _number(raw: Any) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise ValueError("must be a number")
    text = str(raw).replace(",", "").strip()
    if len(text) > 40:
        raise ValueError("must be a number of at most 40 characters")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("must be a number") from exc
    return bounded(value)


def coerce(kind: str, raw: Any) -> Any:
    """Convert a raw value to its typed form, or raise ValueError with the reason."""
    if kind == "money":
        value = _number(raw)
        if value < 0:
            raise ValueError("must be zero or more")
        return value
    if kind == "positive":
        value = _number(raw)
        if value <= 0:
            raise ValueError("must be more than zero")
        return value
    if kind == "rate":
        value = _number(raw)
        if not Decimal(0) <= value <= Decimal(1):
            raise ValueError("must be between 0 and 1")
        return value
    if kind == "date":
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValueError("must be a date written YYYY-MM-DD") from exc
    if kind.startswith("definition:"):
        options = DEFINITIONS[kind.split(":", 1)[1]]
        if raw not in options:
            raise ValueError(f"must be one of: {', '.join(sorted(options))}")
        return raw
    if kind == "brackets":
        return _brackets(raw)
    raise ValueError(f"unknown parameter kind {kind}")


def _brackets(raw: Any) -> tuple[tuple[Decimal | None, Decimal], ...]:
    """Rows of [upper bound, rate]; bounds rise, rates never fall, the last bound is null."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("must be a list of [upper bound, rate] rows")
    rows: list[tuple[Decimal | None, Decimal]] = []
    lower, previous_rate = Decimal(0), Decimal(0)
    for number, row in enumerate(raw, start=1):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError(f"row {number} must be [upper bound, rate]")
        upper_raw, rate_raw = row
        rate = coerce("rate", rate_raw)
        if rate < previous_rate:
            raise ValueError(f"row {number} has a lower rate than row {number - 1}")
        if number == len(raw):
            if upper_raw is not None:
                raise ValueError("the last row's upper bound must be null")
            rows.append((None, rate))
            break
        upper = coerce("positive", upper_raw)
        if upper <= lower:
            raise ValueError(f"row {number} upper bound must be above row {number - 1}")
        rows.append((upper, rate))
        lower, previous_rate = upper, rate
    return tuple(rows)


def build_pack(data: dict[str, Any]) -> tuple[RulePack | None, list[FieldError]]:
    """Validate raw pack data. Returns the pack, or None with every error found."""
    errors: list[FieldError] = []
    raw_params = data.get("parameters")
    if not isinstance(raw_params, dict):
        return None, [FieldError("parameters", "missing")]
    params: dict[str, Parameter] = {}
    for key in raw_params:
        if key not in SCHEMA:
            errors.append(FieldError(key, "unknown parameter"))
    for key, kind in SCHEMA.items():
        entry = raw_params.get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            errors.append(FieldError(key, "missing value"))
            continue
        cite = str(entry.get("cite") or "").strip()
        if len(cite) < 3:
            errors.append(FieldError(f"{key}.cite", "a citation is required"))
        try:
            value = coerce(kind, entry["value"])
        except ValueError as exc:
            errors.append(FieldError(key, str(exc)))
            continue
        params[key] = Parameter(key, value, cite, entry.get("url"))
    if errors:
        return None, errors
    pack = RulePack(
        name=str(data.get("name", "pack")),
        tax_year=int(data.get("tax_year", 0)),
        version=int(data.get("version", 1)),
        parameters=params,
        raw=copy.deepcopy(data),
    )
    return pack, []


def load_pack(path: str | Path) -> RulePack:
    pack, errors = build_pack(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    if pack is None:
        raise PackError(errors)
    return pack


AUTHORITY = re.compile(
    r"\b(IRC|I\.R\.C\.|section|sec\.)\s*\d|§\s*\d|\bNotice\s+\d{4}-\d+|\bRev(\.|enue)\s*(Proc|Rul)|\bPub(\.|lication)?\s*\d+|"
    r"\bTreas(\.|ury)\s*Reg|\b\d+\s*C\.?F\.?R\.?\s*\d|\bP\.?\s?L\.?\s*\d+-\d+|\bPublic Law\s+\d|\b(Form|Schedule)\s+[\w-]+|"
    r"\bFS-\d{4}-\d+|\bIRB\s+\d{4}|\bH\.?\s?R\.?\s*\d+|\bFederal Register\b|https?://",
    re.I,
)
HYPOTHETICAL = re.compile(r"^\s*hypothetical\b", re.I)


def cite_problem(cite: str) -> str:
    """Why a rule edit's citation does not name an authority, or "" when it does or marks a what-if."""
    cite = cite.strip()
    if len(cite) < 3 or HYPOTHETICAL.match(cite) or AUTHORITY.search(cite):
        return ""
    return ("name an authority, such as IRS Notice 2025-69, IRC 225, Rev. Proc. 2024-40 or 29 CFR 778.110, "
            "or write Hypothetical for a what-if")


def is_hypothetical(cite: str) -> bool:
    return bool(HYPOTHETICAL.match(cite or ""))


# Authorities named in a citation, in one normal form, so two citations can be compared: "IRC 225(b)(1)" and
# "section 225" are both "IRC 225"; "IRS Notice 2025-69 p.8" is "Notice 2025-69". A numbered section with a
# decimal ("sec. 3.01" of a revenue procedure) is not a Code section.
_NAMED = (
    (re.compile(r"\b(?:IRC|I\.R\.C\.|Internal Revenue Code(?: section)?|section|sec\.)\s*(\d+[A-Z]?)(?![\d.]*\.\d)|§\s*(\d+[A-Z]?)(?![\d.]*\.\d)", re.I),
     "IRC {}"),
    (re.compile(r"\bNotice\s+(\d{4}-\d+)", re.I), "Notice {}"),
    (re.compile(r"\bRev(?:enue|\.)?\s*Proc(?:edure|\.)?\s*(\d{4}-\d+)", re.I), "Rev. Proc. {}"),
    (re.compile(r"\bRev(?:enue|\.)?\s*Rul(?:ing|\.)?\s*(\d{4}-\d+)", re.I), "Rev. Rul. {}"),
    (re.compile(r"\bPub(?:lication|\.)?\s*(\d+)\b", re.I), "Pub. {}"),
    (re.compile(r"\b(\d+)\s*C\.?F\.?R\.?\s*(?:§\s*)?(\d+\.\d+)", re.I), "{} CFR {}"),
    (re.compile(r"\b(?:P\.?\s?L\.?|Public Law)\s*(\d+-\d+)", re.I), "P.L. {}"),
    (re.compile(r"\bSchedule\s+(1-A|[0-9A-Z]{1,3})\b", re.I), "Schedule {}"),
)
_SUPERSEDED = re.compile(r"\([^)]*supersede[^)]*\)", re.I)


def authorities(cite: str) -> list[str]:
    """The authorities a citation names, in order, each once; an authority it names only as superseded is left out."""
    text = _SUPERSEDED.sub(" ", cite or "")
    found: list[str] = []
    for pattern, form in _NAMED:
        for match in pattern.finditer(text):
            parts = [group for group in match.groups() if group]
            name = form.format(*parts).replace("Schedule 1-a", "Schedule 1-A")
            if name not in found:
                found.append(name)
    return found


def cited_part(text: str) -> str:
    """The part of a phrase that names authorities, with their section, page or parenthetical: "Notice 2025-62, set
    the cap to $10,000" gives "Notice 2025-62", "IRC 225(b)(1), a test" gives "IRC 225(b)(1)". A phrase that names
    none is returned whole."""
    end = max((match.end() for pattern, _ in _NAMED for match in pattern.finditer(text)), default=0)
    if not end:
        return text.strip()
    tail = re.match(r"(?:\s*(?:sec\.|section|§|p\.|pp\.|para\.|part)\s*[\w.()-]+|\s*\([^)]*\))*", text[end:], re.I)
    return text[:end + (tail.end() if tail else 0)].strip(" ,;")


def superseded(cite: str) -> list[str]:
    """The authorities a citation names only as superseded: "(supersedes Rev. Proc. 2024-40 sec. 3.15(1))"."""
    found: list[str] = []
    for part in _SUPERSEDED.findall(cite or ""):
        for name in authorities(part[1:]):
            if name not in found:
                found.append(name)
    return found


# The law that enacted the 2025 amounts these Code sections set: citing it for a different amount contradicts it,
# as citing the section itself does.
_ENACTED = {"IRC 63": "P.L. 119-21", "IRC 151": "P.L. 119-21", "IRC 224": "P.L. 119-21", "IRC 225": "P.L. 119-21"}

# Which kinds of authority can set a parameter. An amount the Code fixes (a cap, the senior amount, a phase-out, the
# 2025 standard deduction) changes only by a law that amends the Code; IRS guidance applies it but cannot change it.
# An amount the IRS adjusts for inflation also changes by revenue procedure. What counts as qualified overtime also
# changes by regulation or IRS guidance. Dates and the Tax Table's limit follow the IRS's instructions; any named
# authority will do for them.
_SET_BY: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("overtime.cap", "overtime.phaseout"), ("P.L. ", "IRC 225"), "IRC 225 sets it, so only a law that amends IRC 225 changes it"),
    (("tips.cap", "tips.phaseout"), ("P.L. ", "IRC 224"), "IRC 224 sets it, so only a law that amends IRC 224 changes it"),
    (("senior.amount", "senior.phaseout"), ("P.L. ", "IRC 151"), "IRC 151(d)(5) sets it, so only a law that amends IRC 151 changes it"),
    (("standard_deduction.single", "standard_deduction.mfj", "standard_deduction.mfs", "standard_deduction.hoh"), ("P.L. ", "IRC 63"),
     "IRC 63(c) sets it, so only a law that amends IRC 63 changes it"),
    (("standard_deduction.age65", "brackets"), ("P.L. ", "Rev. Proc. "),
     "it is the Code's amount adjusted for inflation, so only a revenue procedure or a law changes it"),
    (("overtime.definition",), ("P.L. ", "IRC 225", "Notice ", "Rev. Rul. ", "Rev. Proc. ", "CFR"),
     "only IRC 225, a regulation or IRS guidance (a notice, a ruling or a revenue procedure) defines it"),
)


def _of_kind(name: str, kind: str) -> bool:
    """A Code section matches only itself ("IRC 225", not "IRC 2250"); other kinds match by prefix."""
    if kind == "CFR":
        return " CFR " in name
    return name == kind if kind.startswith("IRC ") else name.startswith(kind)


def authority_problem(pack: RulePack, path: str, cite: str) -> str:
    """Why a citation cannot set this parameter: it names only authorities the rules mark as superseded for it, or
    none of a kind that can set it ("Rev. Proc. 2024-40 cannot set the overtime cap"). "" for a what-if."""
    if len(cite.strip()) < 3 or is_hypothetical(cite):
        return ""  # a missing citation is reported by the pack's own check
    names = authorities(cite)
    label = LABELS.get(path, path)
    old = superseded(pack.cite(path)) if path in pack.parameters else []
    if names and all(name in old for name in names):
        return (f"the rules mark {names[0]} as superseded for the {label}; cite the authority that sets it now, or write "
                "Hypothetical for a what-if")
    for prefixes, kinds, why in _SET_BY:
        if path.startswith(prefixes):
            if any(_of_kind(name, kind) for name in names for kind in kinds):
                return ""
            shown = names[0] if names else "This citation"
            return f"{shown} cannot set the {label}: {why}; cite that authority, or write Hypothetical for a what-if"
    return ""


def contradiction(pack: RulePack, path: str, to: Any, cite: str) -> str:
    """Why a citation cannot support this new value: it names an authority the pack already cites for the
    current, different value (citing IRC 225(b)(1) to set the overtime cap to $9,000 contradicts it). "" when
    the value is unchanged, the parameter is new, or the citation shares no authority with the pack's."""
    if path not in pack.parameters or is_hypothetical(cite):
        return ""
    current = pack.value(path)
    try:
        same = Decimal(str(to)) == Decimal(str(current))
    except (InvalidOperation, ValueError):
        same = str(to) == str(current)
    if same:
        return ""
    current_names = authorities(pack.cite(path))
    current_names += [_ENACTED[name] for name in list(current_names) if name in _ENACTED and _ENACTED[name] not in current_names]
    shared = [name for name in authorities(cite) if name in current_names]
    if not shared:
        return ""
    shown = f"${current:,}" if SCHEMA.get(path) == "money" and isinstance(current, (int, Decimal)) else str(current)
    return (f"{shared[0]} gives the {LABELS.get(path, path)} as {shown}, so it cannot support a different value; cite the authority "
            "that changes it, or write Hypothetical for a what-if")


def _unchanged(pack: RulePack, path: str, to: Any) -> bool:
    """Whether an edit leaves the parameter's value as it is."""
    if path not in pack.parameters:
        return False
    current = pack.value(path)
    try:
        return Decimal(str(to)) == Decimal(str(current))
    except (InvalidOperation, ValueError):
        return str(to) == str(current)


def _shown(pack: RulePack, path: str) -> str:
    current = pack.value(path)
    return f"${current:,}" if SCHEMA.get(path) == "money" and isinstance(current, (int, Decimal)) else str(current)


def edit_pack(pack: RulePack, edits: list[dict[str, Any]]) -> tuple[RulePack | None, list[FieldError]]:
    """Apply rule edits ({path, to, cite, url}) to a copy of the pack and validate the result.

    Every problem is reported: unknown keys, and the value and citation of every edit that names a known key.
    """
    if edits and all(_unchanged(pack, str(edit.get("path", "")), edit.get("to")) for edit in edits):
        # A change that leaves every value as it is (a passage that restates the current limits) changes nothing.
        shown = "; ".join(f"the {LABELS.get(str(edit.get('path')), edit.get('path'))} is already {_shown(pack, str(edit.get('path')))}"
                          for edit in edits)
        return None, [FieldError("rule_edits", f"nothing to change: {shown}")]
    data = copy.deepcopy(pack.raw)
    errors: list[FieldError] = []
    for edit in edits:
        path = edit.get("path", "")
        if path not in SCHEMA:
            errors.append(FieldError(path or "path", "unknown parameter"))
            continue
        entry = {"value": edit.get("to"), "cite": edit.get("cite", "")}
        cite = str(edit.get("cite") or "")
        problem = cite_problem(cite) or contradiction(pack, path, edit.get("to"), cite) or authority_problem(pack, path, cite)
        if problem:
            errors.append(FieldError(f"{path}.cite", problem))
        if edit.get("url"):
            entry["url"] = edit["url"]
        data["parameters"][path] = entry
    data["version"] = pack.version + 1
    new_pack, pack_errors = build_pack(data)
    errors += pack_errors
    return (None, errors) if errors else (new_pack, [])
