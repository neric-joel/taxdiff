from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from taxdiff.facts import ClientReturn
from taxdiff.lanes import llm
from taxdiff.rules import RulePack, edit_pack, load_pack

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_llm_state():
    """No reply cached by one test answers another, and no test uses up another's hourly allowance."""
    llm._REPLIES.clear()
    llm.HOURLY.calls.clear()
    yield


@pytest.fixture(scope="session")
def pack_v1() -> RulePack:
    return load_pack(ROOT / "rules" / "ty2025.yaml")


@pytest.fixture(scope="session")
def pack_v2(pack_v1: RulePack) -> RulePack:
    pack, errors = edit_pack(
        pack_v1, [{"path": "overtime.definition", "to": "flsa_premium", "cite": "IRS Notice 2025-69"}]
    )
    assert not errors
    return pack


def person(role: str = "taxpayer", born: date = date(1985, 6, 1), wages: int = 60_000, **extra: Any) -> dict:
    data = {"role": role, "birth_date": born.isoformat(), "ssn_last4": "1234", "wages": wages}
    data.update({key: (str(value) if isinstance(value, (int, float, Decimal)) else value) for key, value in extra.items()})
    return data


def make_return(filing_status: str = "single", people: list[dict] | None = None, status: str = "prep") -> ClientReturn:
    return ClientReturn.model_validate(
        {"client_id": "C-999", "status": status, "filing_status": filing_status, "people": people or [person()]}
    )
