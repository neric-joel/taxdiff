"""Run PolicyEngine-US live for the dashboard's what-if example and add the results to the parity cache.

The hosted dashboard reads Engine B's results from cache/policyengine.json. `taxdiff parity --refresh`
caches the shipped change sets; this script adds the what-if question the dashboard offers as an example,
from each state the demo can reach it in: the session start, after CS-001, and after CS-001 and C-017's
statement. Results are keyed by each return's inputs and the parameter override, so a question asked from
any of those states finds them. Nothing is applied.

Usage: python scripts/refresh_parity_examples.py   (needs the parity extra; about 30 s per state after the import)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("TAXDIFF_PARITY", "cache")  # replaying the earlier changes reads the cache
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from taxdiff.api.server import EXAMPLES  # noqa: E402
from taxdiff.api.session import Approved, next_change_id, replay  # noqa: E402
from taxdiff.changes import load_change  # noqa: E402
from taxdiff.lanes import questions  # noqa: E402
from taxdiff.paths import CHANGES  # noqa: E402
from taxdiff.workflow import run_change  # noqa: E402


def main() -> int:
    what_if = next(e["question"] for e in EXAMPLES if e["id"] == "what-if")
    fix = Approved(change=load_change(CHANGES / "001-overtime-premium.yaml"), approver="cache refresh")
    statement = Approved(change=load_change(CHANGES / "002-employer-statement-c017.yaml"), approver="cache refresh")
    for history in ([], [fix], [fix, statement]):
        session = replay(history)
        reading = questions.read(what_if, next_change_id(history), session.state.book)
        assert reading.change is not None, reading
        run = run_change(session.state, reading.change, parity_mode="live")
        lane = run.lane("engine_b")
        print(f"after {[h.change.id for h in history] or 'nothing'}: {lane.summary if lane else 'Engine B did not run'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
