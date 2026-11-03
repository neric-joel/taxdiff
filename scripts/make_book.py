"""Build the synthetic TY2025 book: 25 coverage cases written out below, plus 15 seeded filler returns.

Usage: python scripts/make_book.py [--seed 7]
Writes data/book_ty2025.json and data/coverage.json. Every client is synthetic. The seed (default 7) fixes
every drawn value; C-017's SSN digits are written out because the employer statements in data/documents/
carry them. Each filed return records the line values it was filed with, computed by the current engine
under rule pack v1. Those amounts are a record: regenerating the book after an engine change would rewrite
them and hide the drift the session check exists to find, so regenerate only to make a new book.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from taxdiff.engine import compute  # noqa: E402
from taxdiff.facts import ClientReturn  # noqa: E402
from taxdiff.paths import RULES  # noqa: E402
from taxdiff.rules import load_pack  # noqa: E402

# (client, status, filing status, coverage note, people). Each person: (born, wages, extra facts).
COVERAGE = [
    ("C-003", "prep", "single", "1.5x FLSA overtime, W-2 box 14 total only", [("1988-04-12", 58000, {"ot_box14": 6000, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-005", "prep", "mfj", "spouse has 1.5x FLSA overtime", [("1979-09-30", 70000, {}), ("1981-02-14", 48000, {"ot_box14": 9000, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-008", "prep", "single", "employer statement already on file", [("1990-07-07", 52000, {"ot_box14": 4500, "ot_multiple": "1.5", "ot_basis": "flsa", "ot_premium_reported": 1500})]),
    ("C-010", "prep", "mfs", "married filing separately with overtime", [("1984-11-02", 61000, {"ot_box14": 7200, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-012", "review", "single", "1.5x FLSA overtime", [("1986-01-19", 66000, {"ot_box14": 12000, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-014", "review", "hoh", "1.5x FLSA overtime, head of household", [("1983-05-23", 61000, {"ot_box14": 7500, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-016", "review", "single", "2x pay for FLSA overtime hours", [("1991-12-01", 72000, {"ot_box14": 4000, "ot_multiple": "2", "ot_basis": "flsa"})]),
    ("C-019", "review", "single", "mixed FLSA and state overtime, no split", [("1987-03-03", 69000, {"ot_box14": 9600, "ot_multiple": "1.5", "ot_basis": "mixed"})]),
    ("C-021", "review", "single", "overtime with MAGI in the phase-out", [("1980-08-15", 151500, {"ot_box14": 9000, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-017", "filed", "single", "mixed overtime, no split; employer statement arrives later", [("1985-10-10", 71000, {"ot_box14": 7500, "ot_multiple": "1.5", "ot_basis": "mixed", "ssn_last4": "8313"})]),
    ("C-024", "filed", "single", "1.5x FLSA overtime", [("1989-06-06", 64000, {"ot_box14": 8400, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-026", "filed", "mfj", "1.5x FLSA overtime above the single cap", [("1978-02-27", 92000, {"ot_box14": 18000, "ot_multiple": "1.5", "ot_basis": "flsa"}), ("1980-04-04", 40000, {})]),
    ("C-029", "filed", "single", "overtime required only by state law", [("1992-09-09", 57000, {"ot_box14": 5400, "ot_multiple": "1.5", "ot_basis": "state_or_contract"})]),
    ("C-031", "filed", "single", "FLSA hours and regular rate, no box 14 amount", [("1982-12-12", 75000, {"ot_flsa_hours": 200, "ot_regular_rate": 30})]),
    ("C-033", "filed", "hoh", "small overtime amount, change below materiality", [("1990-01-31", 48000, {"ot_box14": 600, "ot_multiple": "1.5", "ot_basis": "flsa"})]),
    ("C-001", "prep", "single", "tipped worker, listed occupation", [("1996-03-15", 41000, {"tips_qualified": 9000, "tips_occupation_listed": True})]),
    ("C-022", "review", "single", "tipped worker, listed occupation", [("1993-11-21", 54000, {"tips_qualified": 14000, "tips_occupation_listed": True})]),
    ("C-035", "filed", "single", "tips, occupation not on the list", [("1995-05-05", 39000, {"tips_qualified": 3000, "tips_occupation_listed": False})]),
    ("C-006", "prep", "single", "born Jan 1 1961: 65 for 2025", [("1961-01-01", 70000, {})]),
    ("C-007", "prep", "single", "born Jan 2 1961: not 65 for 2025", [("1961-01-02", 70000, {})]),
    ("C-018", "review", "mfj", "both spouses 65 or older, senior phase-out", [("1955-03-01", 160000, {}), ("1957-08-09", 0, {})]),
    ("C-027", "filed", "single", "senior in the phase-out", [("1959-05-05", 80000, {})]),
    ("C-011", "prep", "hoh", "head of household, no overtime", [("1987-07-17", 63000, {})]),
    ("C-039", "review", "mfs", "married filing separately, no overtime", [("1983-10-01", 58000, {})]),
    ("C-036", "filed", "mfj", "one spouse 65 or older", [("1958-06-30", 98000, {}), ("1966-02-02", 35000, {})]),
]

FILLER_STATUS = ["prep"] * 6 + ["review"] * 4 + ["filed"] * 5


def person(role: str, born: str, wages: int, extra: dict, rng: random.Random) -> dict:
    data = {"role": role, "birth_date": born, "ssn_last4": f"{rng.randrange(10000):04d}", "wages": str(wages)}
    data.update({key: (str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value) for key, value in extra.items()})
    return data


def build(seed: int) -> dict:
    rng = random.Random(seed)
    returns = []
    for client, status, filing, note, people in COVERAGE:
        roles = ["taxpayer", "spouse"]
        returns.append(
            {
                "client_id": client,
                "status": status,
                "filing_status": filing,
                "people": [person(roles[n], born, wages, extra, rng) for n, (born, wages, extra) in enumerate(people)],
                "_coverage": note,
            }
        )
    used = {item["client_id"] for item in returns}
    free = [f"C-{n:03d}" for n in range(1, 41) if f"C-{n:03d}" not in used]
    statuses = FILLER_STATUS[:]
    rng.shuffle(statuses)
    for client, status in zip(free, statuses, strict=True):
        filing = rng.choice(["single", "single", "mfj", "hoh"])
        count = 2 if filing == "mfj" else 1
        people = []
        for n in range(count):
            born = date(rng.randint(1964, 2001), rng.randint(1, 12), rng.randint(1, 28)).isoformat()
            wages = rng.randrange(28000, 140000, 500) if n == 0 else rng.randrange(0, 60000, 500)
            people.append(person(["taxpayer", "spouse"][n], born, wages, {}, rng))
        returns.append({"client_id": client, "status": status, "filing_status": filing, "people": people, "_coverage": "seeded filler"})
    returns.sort(key=lambda item: item["client_id"])
    coverage = {item["client_id"]: item.pop("_coverage") for item in returns}
    pack = load_pack(RULES)
    for item in returns:
        if item["status"] == "filed":
            values = compute(ClientReturn.model_validate(item), pack).values
            item["as_filed"] = {line: str(value) for line, value in values.items() if line != "tax_formula"}
    return {"tax_year": 2025, "label": "SYNTHETIC", "returns": returns}, coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    book, coverage = build(args.seed)
    out = ROOT / "data" / "book_ty2025.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(book, indent=1) + "\n", encoding="utf-8")
    (ROOT / "data" / "coverage.json").write_text(json.dumps(coverage, indent=1) + "\n", encoding="utf-8")
    counts = {status: sum(r["status"] == status for r in book["returns"]) for status in ("prep", "review", "filed")}
    print(f"wrote {out.relative_to(ROOT)}: {len(book['returns'])} returns {counts}")


if __name__ == "__main__":
    main()
