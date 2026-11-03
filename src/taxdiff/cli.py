"""Command line: show the book, run change sets, refresh the PolicyEngine cache.

  taxdiff book
  taxdiff run changes/001-overtime-premium.yaml changes/002-employer-statement-c017.yaml
  taxdiff run changes/001-overtime-premium.yaml --approve --approver "A. Reviewer"
  taxdiff run changes/900-invalid-negative-cap.yaml
  taxdiff parity --refresh

`python -m taxdiff` runs the same entry point. `run` exits with 0 when every change was previewed or
applied, 1 when any failed validation, was stopped or was not recognized (an instruction the intake lane
refused), and 2 when any run
stopped with an error or an LLM provider was unavailable.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from .changes import ChangeSet, load_change
from .facts import load_book
from .lanes import intake, llm
from .lanes.notes import money, signed, span
from .paths import BOOK, CHANGES, RULES
from .router import ROUTES
from .rules import load_pack
from .workflow import AuditEvent, LaneResult, Run, State, initial_state, record_intake_refusal, run_change, run_json


def _book_table(state: State) -> str:
    rows = [f"{'client':<7} {'status':<7} {'filing':<7} {'AGI':>11} {'Sch 1-A':>11} {'tax':>11}  parity"]
    for client_id, result in sorted(state.results.items()):
        v = result.values
        parity = state.parity[client_id]
        rows.append(
            f"{client_id:<7} {result.status:<7} {result.filing_status:<7} {money(v['agi']):>11} "
            f"{money(v['sch1a_total']):>11} {money(v['tax']):>11}  {parity.state}"
        )
    return "\n".join(rows)


def _drift_line(state: State) -> str:
    filed = len(state.as_filed)
    if not state.drift:
        return f"filed returns: all {filed} recompute to the amounts they were filed with"
    moved = "; ".join(f"{cid} ({', '.join(lines)})" for cid, lines in sorted(state.drift.items()))
    return f"filed returns: {len(state.drift)} of {filed} no longer recompute to their filed amounts: {moved}"


def _print_run(run: Run) -> None:
    print(f"\n{run.change.id}  {run.change.title}")
    for lane in run.lanes:
        print(f"  {lane.lane:<9} {lane.state:<8} {lane.ms:8.1f} ms  {lane.summary}")
    if run.validation and run.validation.errors:
        print("  not applied:")
        for error in run.validation.errors:
            print(f"    - {error}")
        return
    report = run.report
    if report is None:
        return
    verb = "reached" if run.applied else "would reach"
    print(f"  {verb} {len(report.reached)} of {len(report.results)}; not reached {len(report.not_reached)}; unexpected {len(report.unexpected)}")
    for item in report.reached:
        tax = span(item.tax_range) if item.delta_tax is None and item.tax_range else signed(item.delta_tax)
        filed = span(item.filed_range) if item.filed_delta_tax is None and item.filed_range else signed(item.filed_delta_tax)
        print(f"    {item.client_id} {item.status:<6} {ROUTES[item.route]:<26} tax {tax:>10}"
              + (f"  vs filed {filed}" if item.status == "filed" else ""))
    if any(source != "template" for source in run.note_sources.values()):
        print("  notes:")
        for client_id, text in run.notes.items():
            print(f"    {client_id} [{run.note_sources[client_id]}] {text}")


def _print_audit(events: tuple[AuditEvent, ...]) -> None:
    print("\naudit log")
    for event in events:
        approver = f" by {event.approver}" if event.approver else ""
        print(f"  {event.at}  {event.change_id}  {event.outcome}{approver}  pack v{event.pack_version} ({event.pack_sha})  "
              f"materiality {event.materiality or '-'}  {event.parity or 'parity not run'}")
        print(f"    {event.detail}" + (f"; facts {event.facts}" if event.facts else ""))


def _parity_summary(run: Run) -> str:
    lane = run.lane("engine_b")
    if lane is not None:
        return lane.summary
    return "not run: " + "; ".join(str(e) for e in (run.validation.errors if run.validation else []))


def _exit_code(events: tuple[AuditEvent, ...]) -> int:
    outcomes = {event.outcome for event in events}
    return 2 if "error" in outcomes else 1 if outcomes & {"failed validation", "stopped", "not recognized", "refused", "turned down"} else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taxdiff", description="Change-impact triage for prepared tax returns")
    parser.add_argument("--rules", default=str(RULES))
    parser.add_argument("--book", default=str(BOOK))
    parser.add_argument("--parity", choices=["cache", "live", "api", "off"], default="cache")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("book", help="show every return in the book")
    run_cmd = sub.add_parser("run", help="preview change sets in order, or apply them with --approve")
    run_cmd.add_argument("changes", nargs="*", help="change set files, run in order")
    run_cmd.add_argument("--text", help="a written instruction, read by the intake lane and run after the files")
    run_cmd.add_argument("--intake", choices=["pattern", "anthropic", "ollama"], default="pattern")
    run_cmd.add_argument("--notes", choices=["anthropic", "ollama"], help="reword reviewer notes with this LLM")
    run_cmd.add_argument("--materiality", type=Decimal, default=Decimal(50))
    run_cmd.add_argument(
        "--approve",
        action="store_true",
        help="approve and apply each change that passes its checks; without it each change is a preview",
    )
    run_cmd.add_argument("--approver", help="name recorded as the approver in the audit log (default: your user name)")
    run_cmd.add_argument("--json", action="store_true")
    parity_cmd = sub.add_parser("parity", help="run PolicyEngine-US live for the book and the example changes")
    parity_cmd.add_argument("--refresh", action="store_true", required=True)
    args = parser.parse_args(argv)

    pack, book = load_pack(args.rules), load_book(args.book)

    if args.command == "book":
        state = initial_state(pack, book, args.parity)
        print(f"{book.label} book, tax year {book.tax_year}, {pack.label}")
        print(_book_table(state))
        print(_drift_line(state))
        return 0

    if args.command == "parity":
        # Always live, whatever --parity says. Previews apply nothing. The example changes run in file order,
        # each from the state the one before it would apply, as the screen and the README run them; then each
        # rule change runs again on its own against the book as shipped.
        base = initial_state(pack, book, "live")
        print(f"book under {pack.label}: " + ", ".join(f"{s} {sum(p.state == s for p in base.parity.values())}" for s in ("agree", "differs", "not_checked")))
        paths = sorted(CHANGES.glob("0*.yaml"))
        state = base
        for path in paths:
            run = run_change(state, load_change(path), parity_mode="live")
            print(f"{path.name}: {_parity_summary(run)}")
            state = run.pending or state
        for path in paths:
            change = load_change(path)
            if change.kind == "rule":
                print(f"{path.name} alone: {_parity_summary(run_change(base, change, parity_mode='live'))}")
        return 0

    approver = args.approver if args.approver is not None else getpass.getuser()
    if args.approve and not approver.strip():
        parser.error("--approver cannot be blank")
    if not args.changes and args.text is None:
        parser.error("give change set files, --text, or both")
    state = initial_state(pack, book, args.parity)
    output: list[dict] = []
    provider_down = False

    def run_one(change: ChangeSet, intake_lane: LaneResult | None) -> State:
        run = run_change(
            state,
            change,
            approve=args.approve,
            approver=approver,
            materiality=args.materiality,
            parity_mode=args.parity,
            intake=intake_lane,
            notes_provider=args.notes,
        )
        if args.json:
            output.append(run_json(run))
        else:
            _print_run(run)
        # A preview's next state carries forward, so each preview shows the change as if the ones before it
        # were approved; nothing is applied.
        return run.pending or run.state

    for path in args.changes:
        state = run_one(load_change(Path(path)), None)
    if args.text is not None:
        # The written instruction gets id CS-100 and runs after the files, from the state they leave.
        started = perf_counter()
        try:
            proposal = intake.propose(args.text, "CS-100", args.intake)
        except (ValueError, llm.Unavailable) as exc:
            print(f"intake ({args.intake}): {exc}", file=sys.stderr)
            state = record_intake_refusal(state, "CS-100", args.text, args.intake, str(exc))
            provider_down = isinstance(exc, llm.Unavailable)
        else:
            edits = len(proposal.rule_edits) + len(proposal.fact_edits)
            state = run_one(proposal, LaneResult("intake", "done", (perf_counter() - started) * 1000, f"{args.intake}: {edits} {'edit' if edits == 1 else 'edits'}"))
    code = max(_exit_code(state.audit), 2 if provider_down else 0)
    if args.json:
        json.dump({"runs": output, "audit": [asdict(event) for event in state.audit]}, sys.stdout, indent=1)
        print()
        return code
    _print_audit(state.audit)
    if not args.approve:
        print("\nPreview only: each change is shown as if the ones before it were approved, and nothing is applied. "
              "Rerun with --approve to apply them.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
