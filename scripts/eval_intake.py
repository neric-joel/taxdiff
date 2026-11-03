"""Measure how the question reader turns reviewers' requests into readings, with the model and without it.

  python scripts/eval_intake.py                      replay the recorded model replies and print the score
  python scripts/eval_intake.py --record --runs 3    ask the model again, three times per case (needs an
                                                     Anthropic API key and the network), and save its replies

Three sets of cases: tests/eval/intake_cases.json, written with the reader; tests/eval/intake_blind.json,
written by an independent author who did not see the reader's code, and then used to improve the reader, so it
is no longer blind; and tests/eval/intake_holdout.json, written later by another author and never used to change
the reader, which measures how the reader does on wording nobody tuned it on. A replay needs no key and makes no network
call; tests/test_intake_eval.py runs it. Recording also looks up every authority a reading cites at its address
(lanes/law.py check_citation) and saves what it found, so a replay judges fabricated authorities the same way.

A case is correct in a run when the reading matches its expectation; "blocked" cases are correct when the reading
could not be approved (no change, a what-if, a citation that names no authority or contradicts the pack's own
source, an authority not found at its address, or a change that fails validation). A reading counts as wrong and
accepted when the model's reading passed every check, differs from the expected one, and would run (a change
that validates, or an answer about a client or the session): that number must stay at zero. The score is written to data/eval_intake.json, which /api/health returns and the Status page shows.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from taxdiff.changes import validate_change  # noqa: E402
from taxdiff.facts import load_book  # noqa: E402
from taxdiff.lanes import law, llm, questions  # noqa: E402
from taxdiff.paths import BOOK, RULES  # noqa: E402
from taxdiff.rules import cite_problem, is_hypothetical, load_pack  # noqa: E402

SETS = {"authored": ROOT / "tests" / "eval" / "intake_cases.json", "blind": ROOT / "tests" / "eval" / "intake_blind.json",
        "holdout": ROOT / "tests" / "eval" / "intake_holdout.json"}
CASES = SETS["authored"]
REPLIES = ROOT / "tests" / "eval" / "intake_replies.json"
SUMMARY = ROOT / "data" / "eval_intake.json"
UNAVAILABLE = "UNAVAILABLE: "  # a recorded call that raised instead of answering
# Measurements taken before the reader was changed in response to them, published with the current score so the
# score is read with its history: a set used to improve the reader is no longer a blind test.
EARLIER = [
    {"set": "blind", "recorded": "2026-11-02", "cases": 103, "per_run": [77, 77, 77],
     "note": "before the reader was changed for this set's misses"},
    {"set": "holdout", "recorded": "2026-11-02", "cases": 62, "per_run": [49, 50],
     "note": "first measurement, before the reader was changed for its misses; a third run was cut short by the hourly model limit"},
]


def load_cases() -> list[dict[str, Any]]:
    cases = []
    for name, path in SETS.items():
        if path.exists():
            cases += [{**case, "set": name} for case in json.loads(path.read_text(encoding="utf-8"))["cases"]]
    return cases


def _same(value: Any, expected: str) -> bool:
    try:
        return Decimal(str(value).replace(",", "")) == Decimal(str(expected))
    except InvalidOperation:
        return str(value) == str(expected)


def _value(value: Any) -> Any:
    """A value in one form for comparing: a number as a Decimal, anything else as text."""
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return str(value)


def approvable(reading: questions.Reading, pack: Any, book: Any, citations: dict[str, str]) -> bool:
    """Whether a reading could reach an approval: a change that is not a what-if, whose rule edits all name an
    authority that does not contradict the pack and was not reported missing at its address, and that validates."""
    if reading.kind != "change" or reading.change is None or reading.hypothetical:
        return False
    change = reading.change
    for edit in change.rule_edits:
        if not edit.cite.strip() or cite_problem(edit.cite) or is_hypothetical(edit.cite) or citations.get(edit.cite) == "not_found":
            return False
    if change.kind == "rule" and validate_change(change, pack, book).errors:
        return False
    return True


def would_run(reading: questions.Reading, pack: Any, book: Any) -> bool:
    """Whether a reading would be acted on: a change that validates (a document change is read later), or an answer
    about a client or the session. A refusal, or a change that fails validation, is shown as such and runs nothing."""
    if reading.kind == "change" and reading.change is not None:
        return reading.change.kind == "document" or not validate_change(reading.change, pack, book).errors
    return reading.kind in ("explain", "session")


def correct(reading: questions.Reading, expect: dict[str, Any], pack: Any, book: Any, citations: dict[str, str]) -> bool:
    kind = expect["kind"]
    if kind == "blocked":
        return not approvable(reading, pack, book, citations)
    if reading.kind != kind:
        return False
    if kind == "explain":
        return list(reading.clients) == expect["clients"] and reading.line == expect["line"]
    if kind == "session":
        return reading.topic == expect["topic"]
    if kind == "unsupported":
        return True
    change = reading.change
    assert change is not None
    if "all" in expect:  # several facts read together, exactly these
        got = {(edit.client_id, edit.path, _value(edit.to)) for edit in change.fact_edits} if not change.rule_edits else set()
        want = {(item["client"], item["fact"], _value(item["to"])) for item in expect["all"]}
        return got == want
    for option in expect.get("any") or [expect]:
        if "document" in option and change.kind == "document" and change.client_id == option["document"]:
            return True
        if "path" in option and len(change.rule_edits) == 1 and not change.fact_edits:
            edit = change.rule_edits[0]
            what_if = reading.hypothetical or is_hypothetical(edit.cite)
            if edit.path == option["path"] and _same(edit.to, option["to"]) and option.get("what_if", what_if) == what_if:
                return True
        if "fact" in option and len(change.fact_edits) == 1 and not change.rule_edits:
            edit = change.fact_edits[0]
            if (edit.client_id, edit.path) == (option["client"], option["fact"]) and _same(edit.to, option["to"]):
                return True
    return False


def run(cases: list[dict[str, Any]], replies: dict[str, str] | None, citations: dict[str, str] | None = None,
        record: bool = False) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Read every case once. replies=None reads with the pattern reader only; otherwise the model's recorded reply
    stands in for each call (or, recording, the model is asked, every authority a reading cites is looked up, and
    both are kept)."""
    book, pack = load_book(BOOK), load_pack(RULES)
    seen: dict[str, str] = {}
    looked: dict[str, str] = dict(citations or {})
    real_ask = llm.ask
    rows = []
    for case in cases:
        def ask(provider: str, prompt: str, current: str = case["id"], **kwargs: Any) -> str:
            if record:
                llm._REPLIES.clear()  # each run asks afresh: a cached reply would make the runs one run
                try:
                    seen[current] = real_ask(provider, prompt, **kwargs)
                except llm.Unavailable as exc:  # the model declined or failed: replay it the same way
                    seen[current] = f"{UNAVAILABLE}{exc}"
                    raise
                return seen[current]
            if replies is None or current not in replies:
                raise llm.Unavailable("no recorded reply for this case")
            if replies[current].startswith(UNAVAILABLE):
                raise llm.Unavailable(replies[current].removeprefix(UNAVAILABLE))
            return replies[current]

        llm.ask = ask  # type: ignore[assignment]
        try:
            provider = None if replies is None and not record else "anthropic"
            reading = questions.read(case["text"], "CS-100", book, provider, pack=pack)
        finally:
            llm.ask = real_ask  # type: ignore[assignment]
        if record and reading.change is not None:
            for edit in reading.change.rule_edits:
                if edit.cite.strip() and not is_hypothetical(edit.cite) and edit.cite not in looked:
                    looked[edit.cite] = str(law.check_citation(edit.cite)["state"])
        model_read = reading.method == llm.PROVIDERS["anthropic"]
        ok = correct(reading, case["expect"], pack, book, looked)
        rows.append({"id": case["id"], "set": case.get("set", "authored"), "group": case["group"], "ok": ok, "kind": reading.kind,
                     "model_read": model_read, "fallback": reading.fallback,
                     "wrong_accepted": model_read and not ok and would_run(reading, pack, book)})
    return rows, seen, looked


def _set_summary(runs: list[list[dict[str, Any]]], pattern: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [row["id"] for row in runs[0]]
    stable = [i for i in ids if all(next(r for r in rows if r["id"] == i)["ok"] for rows in runs)]
    groups: dict[str, dict[str, int]] = {}
    for row in runs[0]:
        group = groups.setdefault(row["group"], {"cases": 0, "correct": 0})
        group["cases"] += 1
        group["correct"] += row["id"] in stable
    misses = {i: sum(not next(r for r in rows if r["id"] == i)["ok"] for rows in runs) for i in ids if i not in stable}
    return {
        "cases": len(ids),
        "with_model": {
            "correct": len(stable),  # correct in every recorded run
            "per_run": [sum(row["ok"] for row in rows) for rows in runs],
            "read_by_the_model": sum(row["model_read"] for row in runs[0]),
            "refused_by_the_checks": sum(1 for row in runs[0] if row["fallback"].startswith("the model's reading was refused")),
            "wrong_and_accepted": max(sum(row["wrong_accepted"] for row in rows) for rows in runs),
        },
        "pattern_reader_only": {"correct": sum(row["ok"] for row in pattern)},
        "by_group": groups,
        "missed": [f"{i} ({n} of {len(runs)} runs)" for i, n in misses.items()],
    }


def summarize(runs: list[list[dict[str, Any]]], pattern: list[dict[str, Any]], recorded: str) -> dict[str, Any]:
    """The whole score, then each set's. Correct means correct in every recorded run."""
    whole = _set_summary(runs, pattern)
    sets = {}
    for name in dict.fromkeys(row["set"] for row in runs[0]):
        sets[name] = _set_summary([[row for row in rows if row["set"] == name] for rows in runs], [row for row in pattern if row["set"] == name])
    return {"model": llm.ANTHROPIC_MODEL, "recorded": recorded, "runs": len(runs), **whole, "sets": sets, "earlier": EARLIER}


def saved_runs(saved: dict[str, Any]) -> list[dict[str, Any]]:
    return saved.get("runs") or [{"replies": saved.get("replies", {}), "citations": saved.get("citations", {})}]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--record", action="store_true", help="ask the model again and save its replies")
    parser.add_argument("--runs", type=int, default=3, help="recorded runs per case when recording")
    args = parser.parse_args()
    cases = load_cases()
    if args.record:
        if not llm.available()["anthropic"]:
            print("recording needs an Anthropic API key (ANTHROPIC_API_KEY or the local key file)")
            return 2
        recorded: list[dict[str, Any]] = []
        citations: dict[str, str] = {}
        for number in range(args.runs):
            _, seen, citations = run(cases, None, citations, record=True)
            recorded.append({"replies": seen, "citations": citations})
            print(f"run {number + 1}: recorded {len(seen)} replies")
        REPLIES.write_text(json.dumps({"recorded": date.today().isoformat(), "model": llm.ANTHROPIC_MODEL, "runs": recorded},
                                      indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    saved = json.loads(REPLIES.read_text(encoding="utf-8"))
    runs = [run(cases, entry["replies"], entry.get("citations", {}))[0] for entry in saved_runs(saved)]
    pattern, _, _ = run(cases, None, saved_runs(saved)[0].get("citations", {}))
    summary = summarize(runs, pattern, saved["recorded"])
    SUMMARY.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    for row in runs[0]:
        print(f"{row['id']:<5} {'ok  ' if row['ok'] else 'MISS'} {row['kind']:<12} {'model' if row['model_read'] else 'code ':<6} {row['fallback'][:70]}")
    for name, part in summary["sets"].items():
        wm = part["with_model"]
        print(f"{name}: {wm['correct']} of {part['cases']} correct in all {summary['runs']} runs (per run {wm['per_run']}), "
              f"{wm['read_by_the_model']} read by the model, {wm['wrong_and_accepted']} wrong and accepted; "
              f"pattern reader alone {part['pattern_reader_only']['correct']}; missed {', '.join(part['missed']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
