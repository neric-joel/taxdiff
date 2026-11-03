"""Safeguards: lane failures, preview and approval, the audit log, filed amounts, the command line, LLM checks."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from taxdiff import cli, workflow
from taxdiff.changes import ChangeSet, FactEdit, load_change
from taxdiff.facts import apply_fact_edit, load_book
from taxdiff.lanes import engine_b, intake, law, llm, notes
from taxdiff.paths import BOOK, CHANGES, RULES
from taxdiff.rules import load_pack
from taxdiff.workflow import approve_run, initial_state, run_change

OVERTIME_FIX = CHANGES / "001-overtime-premium.yaml"


@pytest.fixture()
def state():
    return initial_state(load_pack(RULES), load_book(BOOK), "off")


# Lane failures --------------------------------------------------------------------------------------


def test_unreadable_parity_cache_leaves_returns_not_checked(tmp_path, monkeypatch):
    broken = tmp_path / "policyengine.json"
    broken.write_text("{not json", encoding="utf-8")
    real = engine_b.load_cache
    monkeypatch.setattr(engine_b, "load_cache", lambda: real(broken))
    state = initial_state(load_pack(RULES), load_book(BOOK), "cache")
    assert all(parity.state == "not_checked" for parity in state.parity.values())
    assert "could not be read" in state.parity_note
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="cache", approve=True)
    assert run.applied and run.lane("engine_b").state == "skipped"


def test_engine_b_that_raises_does_not_stop_the_change(state, monkeypatch):
    def broken(*_):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(engine_b, "run_lane", broken)
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="cache", approve=True)
    assert run.applied
    assert run.lane("engine_b").state == "failed" and "simulated crash" in run.lane("engine_b").summary
    assert all(parity.state == "not_checked" for parity in run.state.parity.values())
    assert "the rest route by status alone" in run.lane("route").summary


def test_unreadable_law_cache_keeps_citations_without_excerpts(tmp_path, monkeypatch):
    broken = tmp_path / "ecfr.json"
    broken.write_text('{"sections": {"29 CFR 778.107": {"heading": "x"}}}', encoding="utf-8")
    monkeypatch.setattr(law, "ECFR_CACHE", broken)
    law.authorities.cache_clear()
    try:
        table = law.authorities()
        assert table["29 CFR 778.107"].excerpt == "" and "notice-2025-69" in table
    finally:
        law.authorities.cache_clear()


def test_engine_a_that_raises_stops_the_change_and_is_audited(state, monkeypatch):
    def broken(*_):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(workflow, "analyze", broken)
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="off", approve=True)
    assert not run.applied and run.state.pack.version == 1
    assert run.state.audit[-1].outcome == "error"


def test_any_unexpected_error_is_audited(state, monkeypatch):
    def broken(*_):
        raise KeyError("simulated")

    monkeypatch.setattr(workflow, "validate_change", broken)
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="off", approve=True)
    assert not run.applied
    assert run.state.audit[-1].outcome == "error" and "unexpected error" in run.state.audit[-1].detail


def test_live_parity_without_policyengine_marks_returns_not_checked(monkeypatch):
    monkeypatch.setattr(engine_b, "installed_version", lambda: "")
    output = engine_b.run_lane(load_book(BOOK), load_pack(RULES), "live")
    assert not output.results
    assert set(output.skipped.values()) >= {"PolicyEngine-US is not installed here (pip install .[parity])"}


def test_live_run_under_another_release_starts_the_cache_again(monkeypatch):
    saved = []
    old = {"policyengine_us": "1.0.0", "results": {"stale": {"x": 1.0}}, "runs": []}
    monkeypatch.setattr(engine_b, "load_cache", lambda: old)
    monkeypatch.setattr(engine_b, "installed_version", lambda: "2.0.0")
    monkeypatch.setattr(engine_b, "save_cache", saved.append)
    monkeypatch.setattr(engine_b, "run_policyengine", lambda units, reform: {key: {name: 0.0 for name in engine_b.COMPARED.values()} for key in units})
    output = engine_b.run_lane(load_book(BOOK), load_pack(RULES), "live")
    assert saved[0]["policyengine_us"] == "2.0.0" and "stale" not in saved[0]["results"]
    assert output.version == "2.0.0"


# Audit ----------------------------------------------------------------------------------------------


def test_audit_records_what_the_change_ran_against(state):
    preview = run_change(state, load_change(OVERTIME_FIX), parity_mode="off")
    applied = approve_run(preview, "A. Reviewer")
    previewed, done = applied.state.audit
    assert (previewed.outcome, done.outcome) == ("previewed", "applied")
    assert done.approver == "A. Reviewer" and done.materiality == "$50" and done.engine.startswith("taxdiff")
    assert done.pack_sha and done.pack_sha != previewed.pack_sha  # v2's content differs from v1's
    assert "off" in done.parity


def test_fact_change_records_facts_versions(state):
    first = run_change(state, load_change(OVERTIME_FIX), parity_mode="off", approve=True)
    second = run_change(first.state, load_change(CHANGES / "002-employer-statement-c017.yaml"), parity_mode="off", approve=True)
    assert second.state.audit[-1].facts == "C-017 v1 to v2"


def test_cli_prints_the_audit_log(capsys):
    cli.main(["--parity", "off", "run", str(OVERTIME_FIX), "--approve", "--approver", "test"])
    out = capsys.readouterr().out
    assert "audit log" in out and "applied by test" in out


# Filed amounts --------------------------------------------------------------------------------------


def test_filed_returns_carry_their_filed_amounts(state):
    assert state.as_filed["C-031"]["tax"] == Decimal("7955")
    assert not state.drift  # every filed return recomputes to its filed amounts under pack v1


def test_starting_from_another_pack_shows_drift_and_keeps_filed_amounts(pack_v2):
    book = load_book(BOOK)
    state = initial_state(pack_v2, book, "off")
    assert set(state.drift) == {"C-017", "C-024", "C-026", "C-029", "C-031", "C-033"}
    assert state.as_filed["C-031"]["tax"] == Decimal("7955")  # the filed amount, not a recomputed one


def test_filed_amounts_are_not_a_fact():
    with pytest.raises(ValueError, match="record of what was filed"):
        apply_fact_edit(load_book(BOOK).get("C-031"), "as_filed.tax", "0")


def test_fact_path_written_with_padded_index_reaches_its_return(state):
    change = ChangeSet(id="CS-800", kind="fact", title="padded", fact_edits=[FactEdit(client_id="C-019", path="people[00].wages", to="70000")])
    run = run_change(state, change, parity_mode="off", approve=True)
    assert run.applied and [item.client_id for item in run.report.reached] == ["C-019"]


# Intake and notes -----------------------------------------------------------------------------------


def _reply(monkeypatch, edits: dict) -> None:
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: json.dumps(edits))


@pytest.mark.parametrize(
    ("text", "edit", "problem"),
    [
        ("Notice 2025-69 sets the cap at 12,500.", {"path": "overtime.cap.other", "to": 15000, "cite": "Notice 2025-69"}, "value 15000"),
        ("The cap is now 12,500.", {"path": "overtime.cap.other", "to": 12500, "cite": "Notice 2026-12"}, "citation"),
        ("The cap is now 12,500 under the new guidance.", {"path": "overtime.cap.other", "to": 12500, "cite": "IRS fact sheet"}, "citation"),
    ],
)
def test_llm_proposal_must_match_the_text(monkeypatch, text, edit, problem):
    _reply(monkeypatch, {"rule_edits": [edit], "fact_edits": []})
    with pytest.raises(ValueError, match=problem):
        intake.propose(text, "CS-160", "ollama")


@pytest.mark.parametrize(
    ("text", "edit"),
    [
        ("Notice 2025-69 sets the cap at $12,500.", {"path": "overtime.cap.other", "to": "12500", "cite": "IRS Notice 2025-69"}),
        ("Per Rev. Proc. 2025-32 the senior phase-out rate becomes 5%.", {"path": "senior.phaseout.rate", "to": 0.05, "cite": "Rev. Proc. 2025-32"}),
    ],
)
def test_llm_proposal_that_matches_the_text_is_kept(monkeypatch, text, edit):
    _reply(monkeypatch, {"rule_edits": [edit], "fact_edits": []})
    change = intake.propose(text, "CS-161", "ollama")
    assert change.rule_edits[0].path == edit["path"] and "found in the text" in change.rationale


def test_llm_fact_proposal_must_name_the_client(monkeypatch):
    _reply(monkeypatch, {"rule_edits": [], "fact_edits": [{"client_id": "C-019", "path": "people[0].ot_flsa_hours", "to": 150}]})
    with pytest.raises(ValueError, match="not named"):
        intake.propose("The employer reports 150 FLSA overtime hours.", "CS-162", "ollama")


def test_note_rewording_must_keep_the_route(monkeypatch):
    note = "C-024 (filed): amendment review. Tax change: -$480. Against the return as filed: -$480."
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: "C-024 (filed): no action needed. Tax change: -$480. Against the return as filed: -$480.")
    text, source = notes.polish(note, "C-024", "ollama", route="Amendment review")
    assert text == note and source.startswith("template")


def test_anthropic_is_offered_only_with_its_package(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "ollama_running", lambda timeout=1.0: False)
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
    assert llm.available() == {"anthropic": False, "ollama": False}


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        (None, "http://localhost:11434"),
        ("0.0.0.0", "http://localhost:11434"),
        ("0.0.0.0:11434", "http://localhost:11434"),
        ("gpu-box", "http://gpu-box:11434"),
        ("https://ollama.example:8443/", "https://ollama.example:8443"),
    ],
)
def test_ollama_host_follows_ollamas_defaults(raw, url):
    assert llm.ollama_host(raw) == url


def test_malformed_llm_reply_is_unreadable(monkeypatch):
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: json.dumps({"rule_edits": {"path": "x"}}))
    with pytest.raises(ValueError, match="not a change set"):
        intake.propose("text", "CS-163", "ollama")


def test_note_rewording_survives_a_provider_crash(monkeypatch):
    def broken(*_, **__):
        raise TypeError("bad reply")

    monkeypatch.setattr(llm, "ask", broken)
    note = "C-003 (in preparation): update the return. Cause: CS-001. Tax change: +$480."
    assert notes.polish(note, "C-003", "ollama", route="Update the return") == (note, "template (the provider failed: TypeError)")


# Preview and approval -------------------------------------------------------------------------------


def test_approval_applies_the_preview_as_it_stands(state):
    preview = run_change(state, load_change(OVERTIME_FIX), parity_mode="off")
    applied = approve_run(preview, "A. Reviewer")
    assert applied.applied and applied.report is preview.report
    assert applied.state.results is preview.pending.results and applied.state.pack.version == 2
    assert [lane.lane for lane in applied.lanes][-2:] == ["approve", "notes"]


def test_blank_approver_is_refused(state):
    preview = run_change(state, load_change(OVERTIME_FIX), parity_mode="off")
    refused = approve_run(preview, "  ")
    assert not refused.applied and refused.state.pack.version == 1
    assert refused.state.audit[-1].outcome == "refused" and "name" in refused.state.audit[-1].detail


def test_only_a_passing_preview_can_be_approved(state):
    rejected = run_change(state, load_change(CHANGES / "900-invalid-negative-cap.yaml"), parity_mode="off")
    with pytest.raises(ValueError):
        approve_run(rejected, "A. Reviewer")


def test_engine_b_and_law_do_not_run_when_engine_a_stops_the_change(state, monkeypatch):
    calls = []

    def broken(*_):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(workflow, "analyze", broken)
    monkeypatch.setattr(engine_b, "run_lane", lambda *args: calls.append(args))
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="cache")
    assert not calls
    assert run.lane("engine_b").state == "skipped" and run.lane("law").state == "skipped"


def test_law_lane_that_raises_does_not_stop_the_change(state, monkeypatch):
    def broken(*_):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(law, "for_rules", broken)
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="off", approve=True)
    assert run.applied and run.lane("law").state == "failed" and run.authorities == []


def test_engine_a_and_route_take_under_one_second(state):
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="off")
    assert run.lane("engine_a").ms + run.lane("route").ms < 1000


def test_engine_release_that_moves_tax_shows_as_drift(monkeypatch):
    import taxdiff.engine as engine

    real = engine.form_tax
    monkeypatch.setattr(engine, "form_tax", lambda brackets, taxable, below: real(brackets, taxable, below) + 1)
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    assert len(state.drift) == 14 and all("tax" in lines for lines in state.drift.values())


def test_a_filed_return_must_record_its_filed_amounts(tmp_path):
    data = json.loads(BOOK.read_text(encoding="utf-8"))
    filed = next(ret for ret in data["returns"] if ret["status"] == "filed")
    del filed["as_filed"]
    (tmp_path / "book.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="as_filed"):
        load_book(tmp_path / "book.json")


def test_document_request_names_the_books_tax_year(state):
    run = run_change(state, load_change(OVERTIME_FIX), parity_mode="off", approve=True)
    assert any(kind == "document_request" and "for tax year 2025." in text for kind, text in run.letters["C-019"])
    assert "Request for" not in run.notes["C-019"]  # the internal note and the letter to the client stay apart


# Command line ---------------------------------------------------------------------------------------


def test_cli_exit_codes(capsys):
    assert cli.main(["--parity", "off", "run", str(OVERTIME_FIX)]) == 0
    assert cli.main(["--parity", "off", "run", str(CHANGES / "900-invalid-negative-cap.yaml")]) == 1
    capsys.readouterr()


def test_cli_end_to_end_matches_the_readme(capsys):
    statement = CHANGES / "002-employer-statement-c017.yaml"
    code = cli.main(["--parity", "cache", "run", str(OVERTIME_FIX), str(statement), "--approve", "--approver", "test", "--json"])
    output = json.loads(capsys.readouterr().out)
    first, second = output["runs"]
    assert code == 0 and first["unexpected"] == [] and len(first["reached"]) == 15
    routes: dict[str, int] = {}
    for row in first["reached"]:
        routes[row["route"]] = routes.get(row["route"], 0) + 1
    assert routes == {"update": 3, "reviewer": 4, "amendment": 5, "document": 1, "below_materiality": 1, "no_change": 1}
    assert [(row["client"], row["route"], row["vs_filed"]) for row in second["reached"]] == [("C-017", "amendment", 1151.0)]
    assert [event["outcome"] for event in output["audit"]] == ["previewed", "applied", "previewed", "applied"]


def test_refused_instruction_still_runs_the_files_and_prints_json(capsys):
    code = cli.main(["--parity", "off", "run", str(OVERTIME_FIX), "--text", "   ", "--json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [run["change"] for run in output["runs"]] == ["CS-001"]
    assert [(event["change_id"], event["outcome"]) for event in output["audit"]] == [("CS-001", "previewed"), ("CS-100", "not recognized")]


def test_refused_instruction_is_audited(monkeypatch, capsys):
    reply = {"rule_edits": [{"path": "overtime.cap.other", "to": 99999, "cite": "Notice 2025-69"}], "fact_edits": []}
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: json.dumps(reply))
    code = cli.main(["--parity", "off", "run", "--text", "Notice 2025-69 keeps the cap at 12,500.", "--intake", "ollama"])
    out = capsys.readouterr().out
    assert code == 1 and "not recognized" in out and "intake:" in out
