"""Edge cases: unknown facts, phase-outs, whole-person edits, bad numbers, keys, provider replies and parity inputs."""

from __future__ import annotations

import io
import json
from datetime import date
from decimal import Decimal

import pytest

from taxdiff.changes import ChangeSet, FactEdit, RuleEdit
from taxdiff.engine import compute
from taxdiff.facts import apply_fact_edit, load_book
from taxdiff.lanes import engine_b, llm, notes
from taxdiff.paths import BOOK, RULES
from taxdiff.provenance import Unknown
from taxdiff.rules import load_pack
from taxdiff.workflow import initial_state, run_change

from .conftest import make_return, person


def test_tips_with_unknown_occupation_ask_for_it(pack_v2):
    ret = make_return(people=[person(wages=52_000, tips_qualified=12_000)])
    result = compute(ret, pack_v2)
    assert isinstance(result.values["sch1a_tips"], Unknown)
    assert "tipped occupation" in result.needs[0]


def test_phase_out_that_removes_the_cap_needs_no_document(pack_v2):
    ret = make_return(people=[person(wages=300_000, ot_box14=9_000, ot_multiple="1.5", ot_basis="mixed")])
    result = compute(ret, pack_v2)
    assert result.values["sch1a_overtime"] == Decimal(0)
    assert not result.needs


def test_replacing_a_whole_person_reaches_the_return():
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    current = state.book.get("C-012").people[0].model_dump(mode="json")
    current["wages"] = "70000"
    change = ChangeSet(id="CS-700", kind="fact", title="replace person", fact_edits=[FactEdit(client_id="C-012", path="people[0]", to=current)])
    run = run_change(state, change, parity_mode="off", approve=True)
    assert run.applied
    assert [item.client_id for item in run.report.reached] == ["C-012"]
    assert run.report.unexpected == []


def test_status_is_not_a_fact():
    ret = load_book(BOOK).get("C-003")
    with pytest.raises(ValueError, match="workflow"):
        apply_fact_edit(ret, "status", "filed")


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "inf"])
def test_non_finite_rule_values_are_rejected(bad):
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    change = ChangeSet(id="CS-701", kind="rule", title="bad", rule_edits=[RuleEdit(path="overtime.cap.other", to=bad, cite="test")])
    run = run_change(state, change, parity_mode="off")
    assert not run.applied
    assert any("overtime.cap.other" in str(error) for error in run.validation.errors)


def test_note_check_catches_a_flipped_sign(monkeypatch):
    note = "C-003 (in preparation): update the return. Tax change: +$480."
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: "C-003 (in preparation): update the return. Tax change: -$480.")
    text, source = notes.polish(note, "C-003", "ollama")
    assert text == note and source.startswith("template")


def test_key_file_with_two_keys_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    key_file = tmp_path / "api key.env"
    key_file.write_text("a = sk-ant-aaaa\nb = sk-ant-bbbb\n", encoding="utf-8")
    monkeypatch.setattr(llm, "KEY_FILE", key_file)
    assert llm.anthropic_key() is None
    key_file.write_text("Key Value = sk-ant-only-one\n", encoding="utf-8")
    assert llm.anthropic_key() == "sk-ant-only-one"


def test_ollama_reply_that_cannot_be_read_is_unavailable(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *_, **__: io.BytesIO(b"not json"))
    with pytest.raises(llm.Unavailable):
        llm.ask("ollama", "hello")


def test_invalid_ssn_returns_are_not_checked():
    book = load_book(BOOK)
    ret = book.get("C-003")
    flagged = apply_fact_edit(ret, "people[0].valid_ssn", False)
    output = engine_b.run_lane(book.replace(flagged), load_pack(RULES), "cache")
    assert output.skipped["C-003"] == "SSN eligibility is not passed to PolicyEngine-US"


def test_live_parity_recomputes_every_checkable_return(monkeypatch):
    saved = []
    computed_keys = []

    def fake_run(units, reform):
        computed_keys.extend(units)
        return {key: {name: 0.0 for name in engine_b.COMPARED.values()} for key in units}

    monkeypatch.setattr(engine_b, "load_cache", lambda: {"results": {}, "runs": []})
    monkeypatch.setattr(engine_b, "installed_version", lambda: "test")
    monkeypatch.setattr(engine_b, "save_cache", saved.append)
    monkeypatch.setattr(engine_b, "run_policyengine", fake_run)
    output = engine_b.run_lane(load_book(BOOK), load_pack(RULES), "live")
    assert len(computed_keys) == len(output.results) == 40  # under rule pack v1 every return is checkable
    assert output.note.startswith("computed 40 returns live")
    assert saved and saved[0]["runs"][-1]["adapter"] == engine_b.ADAPTER_VERSION


def test_cache_key_names_the_tax_year_and_adapter():
    key_now = engine_b.cache_key({"x": 1}, {})
    assert key_now == engine_b.cache_key({"x": 1}, {})
    assert key_now != json.dumps({"x": 1})


def test_born_on_jan_1_1961_gets_the_full_senior_deduction(pack_v2):
    assert compute(make_return(people=[person(born=date(1961, 1, 1), wages=50_000)]), pack_v2).values["sch1a_senior"] == Decimal(6000)
