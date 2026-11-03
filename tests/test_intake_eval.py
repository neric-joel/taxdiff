"""The intake eval: the model's recorded replies to the authored and the blind reviewer requests, three recorded
runs each, replayed through the question reader with every check in place. No key and no network:
scripts/eval_intake.py --record refreshes the replies and the address lookups of the authorities they cite."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_intake", ROOT / "scripts" / "eval_intake.py")
assert spec and spec.loader
eval_intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_intake)


def test_no_wrong_reading_is_accepted_and_the_published_score_is_current():
    cases = eval_intake.load_cases()
    saved = json.loads(eval_intake.REPLIES.read_text(encoding="utf-8"))
    entries = eval_intake.saved_runs(saved)
    runs = [eval_intake.run(cases, entry["replies"], entry.get("citations", {}))[0] for entry in entries]
    pattern, _, _ = eval_intake.run(cases, None, entries[0].get("citations", {}))
    summary = eval_intake.summarize(runs, pattern, saved["recorded"])
    assert summary["with_model"]["wrong_and_accepted"] == 0  # the checks never let a wrong reading through, in any run
    assert summary["runs"] >= 3 and summary["sets"]["blind"]["cases"] >= 100
    assert summary["sets"]["authored"]["with_model"]["correct"] >= 24
    assert summary == json.loads(eval_intake.SUMMARY.read_text(encoding="utf-8"))  # the Status page shows this file
