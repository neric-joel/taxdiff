"""Client facts: the inputs to each return, versioned per client."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

Status = Literal["prep", "review", "filed"]
# Bounds on every amount, hours and rate field: a fact edit with an absurd size or precision fails
# validation with a field reason before any arithmetic or formatting.
LIMIT = Decimal("999999999999.99")
PLACES = 4
FilingStatus = Literal["single", "mfj", "mfs", "hoh"]


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["taxpayer", "spouse"]
    birth_date: date
    ssn_last4: str = Field(pattern=r"^\d{4}$")
    valid_ssn: bool = True
    wages: Decimal = Field(ge=0, le=LIMIT, decimal_places=PLACES, description="W-2 box 1, including overtime pay and tips")
    ot_box14: Decimal | None = Field(default=None, ge=0, le=LIMIT, decimal_places=PLACES, description="Overtime pay shown in W-2 box 14")
    ot_multiple: Decimal | None = Field(default=None, le=LIMIT, decimal_places=PLACES, description="Pay multiple for those hours: 1.5 or 2")
    ot_basis: Literal["flsa", "state_or_contract", "mixed"] | None = None
    ot_premium_reported: Decimal | None = Field(default=None, ge=0, le=LIMIT, decimal_places=PLACES, description="FLSA premium per employer")
    ot_premium_paid: Decimal | None = Field(
        default=None, ge=0, le=LIMIT, decimal_places=PLACES,
        description="Overtime premium paid above the regular rate, per the pay stub (Notice 2025-69 method C at more than 1.5x)",
    )
    ot_flsa_hours: Decimal | None = Field(default=None, ge=0, le=LIMIT, decimal_places=PLACES)
    ot_regular_rate: Decimal | None = Field(default=None, ge=0, le=LIMIT, decimal_places=PLACES)
    tips_qualified: Decimal | None = Field(default=None, ge=0, le=LIMIT, decimal_places=PLACES)
    tips_occupation_listed: bool | None = None

    @field_validator("ot_multiple")
    @classmethod
    def _known_multiple(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value not in (Decimal("1.5"), Decimal("2")):
            raise ValueError("must be 1.5 or 2")
        return value


class ClientReturn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: str = Field(pattern=r"^C-\d{3}$")
    status: Status
    filing_status: FilingStatus
    facts_version: int = Field(default=1, ge=1)
    people: tuple[Person, ...]
    as_filed: dict[str, Decimal] | None = Field(
        default=None, description="Line values on the return as filed, recorded when it was filed; not a fact"
    )

    @model_validator(mode="after")
    def _people_match_filing_status(self) -> ClientReturn:
        roles = [person.role for person in self.people]
        if self.filing_status == "mfj" and roles != ["taxpayer", "spouse"]:
            raise ValueError("a joint return lists the taxpayer, then the spouse")
        if self.filing_status != "mfj" and roles != ["taxpayer"]:
            raise ValueError("this filing status lists the taxpayer only")
        return self


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tax_year: int
    label: str = "SYNTHETIC"
    returns: tuple[ClientReturn, ...]

    @model_validator(mode="after")
    def _filed_returns_record_their_amounts(self) -> Book:
        missing = [ret.client_id for ret in self.returns if ret.status == "filed" and not ret.as_filed]
        if missing:
            raise ValueError(f"a filed return records the amounts it was filed with (as_filed); missing for {', '.join(missing)}")
        return self

    def get(self, client_id: str) -> ClientReturn | None:
        return next((ret for ret in self.returns if ret.client_id == client_id), None)

    def replace(self, updated: ClientReturn) -> Book:
        returns = tuple(updated if ret.client_id == updated.client_id else ret for ret in self.returns)
        return self.model_copy(update={"returns": returns})


def load_book(path: str | Path) -> Book:
    return Book.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


FACT_LABELS: dict[str, str] = {
    "wages": "W-2 box 1 wages",
    "birth_date": "birth date",
    "valid_ssn": "valid SSN",
    "ssn_last4": "SSN last four",
    "ot_box14": "W-2 box 14 overtime pay",
    "ot_multiple": "overtime pay multiple",
    "ot_basis": "overtime basis",
    "ot_premium_reported": "FLSA premium reported by the employer",
    "ot_premium_paid": "overtime premium paid above the regular rate",
    "ot_flsa_hours": "FLSA overtime hours",
    "ot_regular_rate": "regular rate of pay",
    "tips_qualified": "qualified tips",
    "tips_occupation_listed": "tipped occupation on the IRS list",
    "filing_status": "filing status",
    "people": "number of people on the return",
}


# The document a firm takes each fact from (in this synthetic book, the document it stands for).
FACT_DOCUMENTS: dict[str, str] = {
    "wages": "Form W-2",
    "ot_box14": "Form W-2",
    "ot_multiple": "pay stubs",
    "ot_basis": "pay stubs and the employer's overtime policy",
    "ot_premium_reported": "employer statement",
    "ot_premium_paid": "pay stubs",
    "ot_flsa_hours": "pay stubs or employer statement",
    "ot_regular_rate": "pay stubs or employer statement",
    "tips_qualified": "Form W-2 and tip records",
    "tips_occupation_listed": "employer's job description",
    "birth_date": "client organizer",
    "valid_ssn": "client organizer",
    "ssn_last4": "client organizer",
    "filing_status": "client organizer",
    "people": "client organizer",
}


def fact_document(path: str) -> str:
    """The document a fact comes from: people[0].ot_box14 comes from "Form W-2"."""
    return FACT_DOCUMENTS.get(str(parse_path(path)[-1]), "client file")


def fact_label(path: str) -> str:
    """A fact path in words: people[1].ot_box14 reads "spouse's W-2 box 14 overtime pay"."""
    parts = parse_path(path)
    name = FACT_LABELS.get(str(parts[-1]), str(parts[-1]))
    if len(parts) >= 3 and parts[0] == "people":
        return f"{'taxpayer' if parts[1] == 0 else 'spouse'}'s {name}"
    return name


_STEP = re.compile(r"([a-z_0-9]+)(?:\[(\d+)\])?")


def parse_path(path: str) -> list[str | int]:
    """Split "people[0].ot_box14" into ["people", 0, "ot_box14"]."""
    parts: list[str | int] = []
    for segment in path.split("."):
        match = _STEP.fullmatch(segment)
        if not match:
            raise ValueError(f"cannot read path segment {segment!r}")
        parts.append(match.group(1))
        if match.group(2) is not None:
            parts.append(int(match.group(2)))
    return parts


def normal_path(path: str) -> str:
    """Write a fact path the way the engine records it: people[00].wages becomes people[0].wages."""
    out = ""
    for part in parse_path(path):
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def apply_fact_edit(ret: ClientReturn, path: str, value: Any) -> ClientReturn:
    """Return a new version of the return with one fact changed. Raises ValueError or ValidationError."""
    data = ret.model_dump(mode="json")
    parts = parse_path(path)
    if parts[0] in ("client_id", "facts_version"):
        raise ValueError(f"{path} cannot be edited")
    if parts[0] == "status":
        raise ValueError("a return's status is workflow, not a tax fact; it is not edited by a change set")
    if parts[0] == "as_filed":
        raise ValueError("the amounts on a filed return are a record of what was filed, not a fact to edit")
    target: Any = data
    for part in parts[:-1]:
        target = target[part]
    last = parts[-1]
    if isinstance(target, dict) and last not in target:
        raise ValueError(f"{path} is not a known fact")
    target[last] = value
    data["facts_version"] = ret.facts_version + 1
    return ClientReturn.model_validate(data)


def validation_messages(error: ValidationError) -> list[tuple[str, str]]:
    """Field-level messages from a pydantic error, as (field, message) pairs."""
    out = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "value"
        out.append((field, item["msg"]))
    return out
