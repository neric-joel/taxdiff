"""Lanes: law excerpts, intake providers, the notes figure check, documents, routing with parity."""

from __future__ import annotations

import json
from decimal import Decimal
from time import perf_counter

import pytest

from taxdiff import workflow
from taxdiff.changes import load_change
from taxdiff.facts import load_book
from taxdiff.lanes import documents, intake, law, llm, notes
from taxdiff.paths import BOOK, CHANGES, ROOT, RULES
from taxdiff.router import route
from taxdiff.rules import load_pack
from taxdiff.workflow import initial_state, run_change

NOTE = (
    "C-003 (in preparation): update the return. Cause: CS-001 Count only the FLSA-required overtime premium. "
    "Qualified overtime, Schedule 1-A part III: $6,000 to $2,000. Tax change: +$480."
)


def test_law_excerpts_come_from_the_ecfr_cache():
    table = law.authorities()
    section = table["29 CFR 778.107"]
    assert section.excerpt.startswith("The general overtime pay standard")
    assert section.as_of == "2026-10-29"
    assert [a.key for a in law.for_rules(["overtime.definition"])][-2:] == ["29 CFR 778.107", "29 CFR 778.110"]


def test_llm_intake_proposal_is_parsed(monkeypatch):
    reply = {"rule_edits": [{"path": "overtime.cap.other", "to": 10000, "cite": "Notice 2099-01"}], "fact_edits": []}
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: json.dumps(reply))
    change = intake.propose("the cap becomes 10,000 per Notice 2099-01", "CS-150", "ollama")
    assert change.kind == "rule" and change.rule_edits[0].path == "overtime.cap.other"
    assert "pending approval" in change.author


@pytest.mark.parametrize(
    "reply",
    [
        "no json here",
        json.dumps({"rule_edits": [], "fact_edits": []}),
        json.dumps({"rule_edits": [{"path": "tips.cap", "to": 1, "cite": "x"}], "fact_edits": [{"client_id": "C-001", "path": "people[0].wages", "to": 1}]}),
    ],
)
def test_llm_intake_rejects_unusable_replies(monkeypatch, reply):
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: reply)
    with pytest.raises(ValueError):
        intake.propose("text", "CS-151", "anthropic")


@pytest.mark.parametrize(
    ("reply", "kept"),
    [
        ("After CS-001 Count only the FLSA-required overtime premium, qualified overtime on Schedule 1-A part III goes from $6,000 to $2,000; tax change +$480.", True),
        ("After CS-001, qualified overtime on Schedule 1-A part III goes from $2,000 to $6,000; tax change +$480.", False),
        ("After CS-001, qualified overtime on Schedule 1-A part III goes from $6,000 to $2,000.", False),
        ("After CS-001, overtime on Schedule 1-A part III goes from $6,000 to $2,000; tax change +$480, about 8%.", False),
        ("After CS-001, qualified overtime on Schedule 1-A part III goes from $6,000 to $2,000; tax change -$480.", False),
    ],
)
def test_note_rewording_keeps_the_decision_and_every_figure(monkeypatch, reply, kept):
    # The model sees and rewords only what follows the decision sentence, which stays as the template wrote it.
    asked: list[str] = []
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: asked.append(prompt) or reply)
    text, source = notes.polish(NOTE, "C-003", "ollama", route="Update the return")
    assert "update the return" not in asked[0].split("Note:", 1)[1]
    assert (text == "C-003 (in preparation): update the return. " + reply) is kept
    assert source.startswith("LLM") is kept


def test_note_rewording_falls_back_when_the_provider_is_unavailable(monkeypatch):
    def unavailable(*_, **__):
        raise llm.Unavailable("no server")

    monkeypatch.setattr(llm, "ask", unavailable)
    assert notes.polish(NOTE, "C-003", "ollama") == (NOTE, "template (no server)")


def test_letters_to_clients_are_kept_apart_and_never_reworded(monkeypatch):
    monkeypatch.setattr(workflow, "NOTES_REWORD_MAX", 100)
    asked = []
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: asked.append(prompt) or prompt.split("Note: ", 1)[1])
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    run = run_change(state, load_change(CHANGES / "001-overtime-premium.yaml"), parity_mode="off", notes_provider="ollama", approve=True)
    assert run.note_sources["C-003"] == "LLM wording, figures checked"  # internal notes may be reworded
    kinds = {cid: [kind for kind, _ in found] for cid, found in run.letters.items()}
    assert kinds == {"C-017": ["client_notice", "document_request"], "C-019": ["document_request"], "C-024": ["client_notice"],
                     "C-026": ["client_notice"], "C-029": ["client_notice"], "C-031": ["client_notice"], "C-033": ["client_notice"]}
    letters = [text for found in run.letters.values() for _, text in found]
    assert not any(text in prompt for text in letters for prompt in asked)  # no letter was sent to the model
    assert not any("Notice for" in note or "Request for" in note for note in run.notes.values())
    # each notice says what the error means for the client: interest on more tax, the penalty only where the amount
    # could draw it ($672 cannot be a substantial understatement), the refund window
    c024 = dict(run.letters["C-024"])["client_notice"]
    assert "interest runs on it from April 15, 2026" in c024 and "does not apply (IRC 6662(d)(1)(A))" in c024
    assert "Treas. Reg. 1.6664-2(c)" not in c024 and "$0" not in c024
    assert "within 3 years after the date you filed the return" in dict(run.letters["C-031"])["client_notice"]


def test_a_letter_names_the_penalty_only_where_the_amount_can_draw_it():
    small, large = notes._consequences(True, 2025, Decimal(48)), notes._consequences(True, 2025, Decimal(6200))
    assert "does not apply (IRC 6662(d)(1)(A))" in small and "1.6664-2(c)" not in small
    assert "accuracy-related penalty (IRC 6662)" in large and "Treas. Reg. 1.6664-2(c)" in large
    # an unknown increase (a document is still out) keeps the warning
    assert "Treas. Reg. 1.6664-2(c)" in notes._consequences(True, 2025, None)


def test_pdf_statement_reads_through_the_cached_conversion():
    text, method = documents.read_text(ROOT / "data" / "documents" / "employer-ot-statement-c017.pdf")
    assert method.startswith("Docling (cached conversion)")
    statement = documents.read_statement(text, method, "C-017", load_book(BOOK))
    assert not statement.errors
    assert {edit.path: edit.to for edit in statement.edits} == {
        "people[0].ot_premium_reported": "1920.00",
        "people[0].ot_flsa_hours": "160.00",
        "people[0].ot_regular_rate": "24.00",
    }


def test_pdf_change_set_matches_the_text_one():
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    first = run_change(state, load_change(CHANGES / "001-overtime-premium.yaml"), parity_mode="off", approve=True).state
    text_run = run_change(first, load_change(CHANGES / "002-employer-statement-c017.yaml"), parity_mode="off")
    pdf_run = run_change(first, load_change(CHANGES / "004-employer-statement-c017-pdf.yaml"), parity_mode="off")
    assert text_run.report.reached[0].after.values == pdf_run.report.reached[0].after.values


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("filed", True, True, None, False), "amendment"),  # a filed return with a missing fact: both, the request is written too
        (("filed", False, False, None, False), "no_change"),
        (("filed", True, False, Decimal(500), True), "reviewer"),
        (("prep", True, False, None, False), "update"),
        (("review", True, False, None, False), "reviewer"),
        (("filed", True, False, Decimal(49), False), "below_materiality"),
        (("filed", True, False, Decimal(-50), False), "amendment"),
        (("filed", True, False, Decimal("50.00"), False), "amendment"),  # materiality itself is material
        (("filed", True, False, Decimal("-49.99"), False), "below_materiality"),
        (("filed", True, True, None, True), "amendment"),  # Unknown comes before an engine disagreement
        (("review", True, False, None, True), "reviewer"),
        (("prep", False, True, None, False), "no_change"),  # still waiting on an earlier request: nothing new to do
    ],
)
def test_routing_table(args, expected):
    status, changed, unknown, filed_delta, differ = args
    assert route(status, changed, unknown, filed_delta, Decimal(50), engines_differ=differ) == expected


def test_a_document_that_contradicts_a_filed_return_goes_to_amendment_review():
    assert route("filed", False, False, Decimal(0), Decimal(50), conflict=True) == "amendment"
    assert route("review", False, False, None, Decimal(50), conflict=True) == "reviewer"


def test_at_most_eight_notes_are_reworded_per_approval(monkeypatch):
    monkeypatch.setattr(workflow, "NOTES_REWORD_MAX", 5)  # CS-001 writes 15 internal notes; a lower cap shows the limit
    asked = []
    monkeypatch.setattr(llm, "ask", lambda provider, prompt, **_: asked.append(prompt) or prompt.split("Note: ", 1)[1])
    state = initial_state(load_pack(RULES), load_book(BOOK), "off")
    run = run_change(state, load_change(CHANGES / "001-overtime-premium.yaml"), parity_mode="off", notes_provider="ollama", approve=True)
    reworded = [cid for cid, source in run.note_sources.items() if source.startswith("LLM")]
    capped = [cid for cid, source in run.note_sources.items() if "at most 5 notes" in source]
    assert len(asked) == len(reworded) == workflow.NOTES_REWORD_MAX == 5 and len(capped) == 10


def test_model_calls_follow_the_run_they_serve(monkeypatch):
    timeouts = []
    monkeypatch.setattr(llm, "_ask", lambda provider, prompt, **kw: timeouts.append(kw["timeout"]) or "reply")
    for guard, expected in ((llm.Guard(deadline=perf_counter() + 60, stopped=lambda: True), "stopped"),
                            (llm.Guard(deadline=perf_counter() + 1), "too little time")):
        token = llm.GUARD.set(guard)
        try:
            with pytest.raises(llm.Unavailable, match=expected):
                llm.ask("anthropic", expected)
        finally:
            llm.GUARD.reset(token)
    token = llm.GUARD.set(llm.Guard(deadline=perf_counter() + 10))
    try:
        assert llm.ask("anthropic", "in time") == "reply" and timeouts == [pytest.approx(9, abs=0.5)]
    finally:
        llm.GUARD.reset(token)


def test_a_repeated_prompt_reuses_its_reply_and_the_hourly_limit_holds(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_ask", lambda provider, prompt, **kw: calls.append(prompt) or f"reply {len(calls)}")
    assert llm.ask("anthropic", "same") == llm.ask("anthropic", "same") == "reply 1" and calls == ["same"]
    monkeypatch.setattr(llm, "HOURLY", llm._Hourly(2))
    llm.ask("anthropic", "one")
    llm.ask("anthropic", "two")
    with pytest.raises(llm.Unavailable, match="an hour is used up"):
        llm.ask("anthropic", "three")


def test_engine_b_asks_the_policyengine_api_once_per_request(monkeypatch):
    from taxdiff.lanes import engine_b

    book, pack = load_book(BOOK), load_pack(RULES)
    cache = engine_b.load_cache()
    sent = []

    def answer(body, timeout):
        sent.append(body)
        units = body["household"]["tax_units"]
        # answer with the cached results for each unit, so agreement is known
        by_key = {key: cache["results"][key] for key in units}
        return {"status": "ok", "result": {"tax_units": {key: {name: {"2025": by_key[key][name]} for name in engine_b.COMPARED.values()}
                                                         for key in units}}}

    engine_b._ANSWERS.clear()
    monkeypatch.setattr(engine_b, "_post", answer)
    first = engine_b.run_lane(book, pack, "api")
    again = engine_b.run_lane(book, pack, "api")
    assert first.source == again.source == "api" and not first.fallback and len(first.results) == 40
    assert len(sent) == 1  # the replayed request is answered from memory
    assert all(name in sent[0]["household"]["tax_units"][key] for key in sent[0]["household"]["tax_units"] for name in engine_b.COMPARED.values())


def test_engine_b_falls_back_to_the_cache_when_the_api_does_not_answer(monkeypatch):
    from taxdiff.lanes import engine_b

    def slow(body, timeout):
        raise TimeoutError("timed out")

    engine_b._ANSWERS.clear()
    monkeypatch.setattr(engine_b, "_post", slow)
    output = engine_b.run_lane(load_book(BOOK), load_pack(RULES), "api")
    assert output.source == "cache" and "did not answer within" in output.fallback and len(output.results) == 40
