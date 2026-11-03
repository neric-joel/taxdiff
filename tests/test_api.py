"""The web API: the same results as the command line, streamed node by node, with its limits and fallbacks.

Every test runs with no LLM key and with the eCFR API unreachable (both are stubbed), so the suite makes no
network call. Tests that need a model stub `llm.ask` and offer the provider through `server._provider`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taxdiff.api import pipeline, server
from taxdiff.cli import main
from taxdiff.lanes import explain, law, llm

ROOT = Path(__file__).resolve().parents[1]
CHANGES = sorted((ROOT / "changes").glob("*.yaml"))
GRAPH = ("question", "intake", "documents", "validate", "engine_a", "engine_b", "law", "route", "approve", "notes", "explain", "audit")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def unreachable(url, timeout):
        raise OSError("network is off in tests")

    monkeypatch.setattr(law, "_get", unreachable)
    monkeypatch.setattr(law, "_status", lambda url, timeout: None)  # a cited authority's address gives no answer
    monkeypatch.setattr(law, "_as_of", None)
    monkeypatch.setattr(llm, "anthropic_key", lambda: None)
    monkeypatch.setattr(llm, "ollama_running", lambda timeout=1.0: False)
    monkeypatch.setenv("TAXDIFF_LLM", "anthropic")
    monkeypatch.setattr(server, "RUNS", server.Throttle(1000, 600))
    monkeypatch.setattr(server, "LLM_RUNS", server.Throttle(1000, 600))
    server._checks.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


def stream(client, path, body):
    with client.stream("POST", path, json=body) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.iter_lines() if line]


def result_of(events):
    return next(e["result"] for e in events if e["type"] == "result")


def ask(client, question, history=(), materiality="50"):
    events = stream(client, "/api/run", {"question": question, "history": list(history), "materiality": materiality})
    return events, result_of(events)


def decide(client, result, history=(), decision="approve", approver="A. Reviewer", token=None):
    body = {"question": result["question"], "change": result["change"], "history": list(history), "materiality": result["materiality"],
            "approver": approver, "decision": decision, "preview_token": token or result["preview_token"], "hypothetical": result["hypothetical"]}
    events = stream(client, "/api/approve", body)
    return events, result_of(events)


def apply(client, question, history=()):
    """Preview a question's change and approve it; returns the applied result."""
    _, preview = ask(client, question, history)
    assert preview["status"] == "waiting", preview
    _, applied = decide(client, preview, history)
    assert applied["status"] == "applied", applied
    return applied


def cli_runs(capsys, *paths, approve=True):
    capsys.readouterr()
    main(["--parity", "cache", "run", *map(str, paths), *(["--approve", "--approver", "A. Reviewer"] if approve else []), "--json"])
    return json.loads(capsys.readouterr().out)["runs"]


def comparable(run):
    return {key: value for key, value in run.items() if key != "lanes"}


def nodes(events):
    return [e for e in events if e["type"] == "node"]


# Same results as the command line --------------------------------------------------------------------


@pytest.mark.parametrize("path", CHANGES, ids=lambda p: p.stem)
def test_each_change_set_gives_the_cli_result(client, capsys, path):
    change_id = path.stem.split("-")[0]
    _, preview = ask(client, f"Run CS-{change_id}")
    # a what-if (a hypothetical citation) is previewed only, on the web and on the command line
    expected = cli_runs(capsys, path, approve=preview["status"] != "what_if")[0]
    if preview["status"] != "waiting":
        assert comparable(preview["cli"]) == comparable(expected)
        return
    _, applied = decide(client, preview)
    assert comparable(applied["cli"]) == comparable(expected)


def test_a_sequence_of_changes_gives_the_cli_result(client, capsys):
    expected = cli_runs(capsys, ROOT / "changes" / "001-overtime-premium.yaml", ROOT / "changes" / "002-employer-statement-c017.yaml")
    first = apply(client, "Run CS-001")
    second = apply(client, "Run CS-002", [first["history_item"]])
    assert [comparable(first["cli"]), comparable(second["cli"])] == [comparable(run) for run in expected]


def test_figures_on_the_dashboard_equal_the_cli_figures(client, capsys):
    expected = {row["client"]: row for row in cli_runs(capsys, ROOT / "changes" / "001-overtime-premium.yaml")[0]["reached"]}
    applied = apply(client, "Run CS-001")
    assert {row["client"] for row in applied["impact"]} == set(expected)
    for row in applied["impact"]:
        cli = expected[row["client"]]
        assert row["route"] == cli["route"]
        if cli["tax_change"] is None:
            assert row["tax_change"]["value"] is None
        else:
            assert Decimal(row["tax_change"]["value"]) == Decimal(str(cli["tax_change"]))
        if cli["vs_filed"] is not None:
            assert Decimal(row["vs_filed"]["value"]) == Decimal(str(cli["vs_filed"]))
    tax_after = applied["cli"]["tax_after"]
    for row in applied["book"]:
        assert (row["tax"]["value"] or "") == ("" if tax_after[row["client"]] == "Unknown" else tax_after[row["client"]])
    assert applied["kpis"]["routes"] == {"update": 3, "reviewer": 4, "amendment": 5, "document": 1, "below_materiality": 1, "no_change": 1}
    c003 = next(row for row in applied["impact"] if row["client"] == "C-003")
    assert c003["tax_change"]["text"] == "+$480"


def test_the_statement_routes_c017_to_amendment_review(client):
    first = apply(client, "Run CS-001")
    _, preview = ask(client, "C-017's employer sent a corrected overtime statement", [first["history_item"]])
    assert preview["change"]["kind"] == "document" and preview["change"]["document"] == "data/documents/employer-ot-statement-c017.txt"
    row = preview["impact"][0]
    assert (row["client"], row["route"], row["vs_filed"]["text"]) == ("C-017", "amendment", "+$1,151")


# Rejected input --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("question", "fields"), [
    ("Run CS-900", {"overtime.cap.other", "overtime.cap.other.cite"}),
    ("Run CS-901", {"tax_year"}),
])
def test_invalid_change_sets_are_rejected_with_field_reasons(client, question, fields):
    events, result = ask(client, question)
    assert result["status"] == "rejected"
    assert {error["field"] for error in result["errors"]} == fields
    assert result["audit"][-1]["outcome"] == "failed validation"
    states = {e["node"]: e["state"] for e in nodes(events)}
    assert states["engine_a"] == "skipped" and states["approve"] == "skipped"
    assert result["answer"]["headline"].endswith("failed validation. Nothing changed.")


def test_a_rejection_keeps_the_last_valid_version(client):
    first = apply(client, "Run CS-001")
    _, rejected = ask(client, "Run CS-900", [first["history_item"]])
    assert rejected["status"] == "rejected"
    assert "Rules version 2, unchanged" in rejected["answer"]["text"]


def test_a_rule_change_without_a_citation_is_rejected(client):
    _, result = ask(client, "Set the overtime cap to $10,000")
    assert result["status"] == "rejected"
    assert {e["field"] for e in result["errors"]} == {"overtime.cap.other.cite"}


def test_a_question_the_tool_cannot_run_says_what_it_can_do(client):
    events, result = ask(client, "What is the weather in Irvine?")
    assert result["status"] == "unsupported"
    assert result["can_do"] and result["answer"]["headline"] == "This prototype cannot run that question."
    tries = next(section for section in result["answer"]["sections"] if section.get("title") == "Try one of these")
    assert all(item[0]["target"]["question"] == item[0]["text"] for item in tries["items"])
    assert {e["node"] for e in nodes(events) if e["state"] == "skipped"} >= {"validate", "engine_a", "approve"}


def test_an_instruction_the_intake_refuses_is_audited(client):
    events, result = ask(client, "set no.such.rule to 1 per Nothing")
    assert result["status"] == "unsupported"
    assert result["audit"][-1]["outcome"] == "not recognized" and result["audit"][-1]["detail"].startswith("intake:")
    assert any(e["node"] == "audit" and e["state"] == "done" for e in nodes(events))


# The event stream ------------------------------------------------------------------------------------


def test_the_stream_reports_every_node_in_order_with_measured_durations(client):
    events, preview = ask(client, "Run CS-001")
    _, applied = decide(client, preview)
    seen = nodes(events) + nodes(stream(client, "/api/approve", {
        "change": preview["change"], "history": [], "materiality": "50", "approver": "B. Reviewer", "decision": "approve",
        "preview_token": preview["preview_token"]}))
    assert {e["node"] for e in seen} == set(GRAPH)
    for event in seen:
        assert event["run_id"] and isinstance(event["t"], (int, float))
        if event["state"] in ("done", "failed", "skipped", "waiting"):
            assert isinstance(event["duration_ms"], (int, float)) and event["duration_ms"] >= 0
            assert event["started_at"]
    finished = [e["node"] for e in nodes(events) if e["state"] in ("done", "skipped", "waiting")]
    assert finished[-1] == "audit"  # the preview's audit entry, written as the preview ends
    finished = finished[:-1]
    order = [n for n in GRAPH if n in finished]
    assert [n for n in finished if n not in ("engine_b", "law")] == [n for n in order if n not in ("engine_b", "law")]
    assert finished.index("engine_a") < finished.index("engine_b") < finished.index("route")
    assert finished.index("engine_a") < finished.index("law") < finished.index("route")
    queued = [e["node"] for e in nodes(events) if e["state"] == "queued"]
    assert queued == ["engine_b", "law"]
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    assert events[-1]["status"] == "waiting_for_approval"
    assert applied["status"] == "applied"


def test_a_question_about_a_client_skips_validation_and_approval(client):
    first = apply(client, "Run CS-001")
    events, result = ask(client, "Why did C-031's tax go down?", [first["history_item"]])
    states = {e["node"]: (e["state"], e["summary"]) for e in nodes(events) if e["state"] != "running"}
    assert states["validate"][0] == "skipped" and "nothing to validate" in states["validate"][1]
    assert states["approve"][0] == "skipped" and "nothing to approve" in states["approve"][1]
    assert result["status"] == "answered" and result["client"] == "C-031"
    assert "went down $660" in result["answer"]["text"] and "CS-001" in result["answer"]["text"]
    assert result["chain"]["line"] == "tax" and result["chain"]["uses"]


def test_a_what_if_that_moves_nothing_says_why(client):
    # After CS-001 only the premium counts, so no single filer's overtime deduction reaches a $10,000 cap.
    first = apply(client, "Run CS-001")
    _, result = ask(client, "What if the overtime cap drops to $10,000?", [first["history_item"]])
    assert result["status"] == "what_if" and result["answer"]["headline"] == "No return's tax would change."
    assert ("12 of 40 returns use the edited rule, and none of them would move: the largest overtime deduction among them is "
            "$4,000 (C-012), already within the new $10,000 cap.") in result["answer"]["text"]


def test_a_yes_or_no_question_is_answered_from_the_results(client):
    first = apply(client, "Run CS-001")
    _, result = ask(client, "Did C-031's tax go down?", [first["history_item"]])
    assert result["status"] == "answered" and result["client"] == "C-031" and "went down $660" in result["answer"]["text"]


def test_a_false_premise_is_corrected_from_the_results(client):
    first = apply(client, "Run CS-001")
    second = apply(client, "C-017's employer sent a corrected overtime statement", [first["history_item"]])
    _, result = ask(client, "Why did C-017's tax go down?", [first["history_item"], second["history_item"]])
    assert result["answer"]["headline"] == "C-017's tax did not go down: it went up $1,151, from $5,495 to $6,646."
    assert "$1,151" in result["answer"]["text"]


def test_each_answer_figure_links_to_its_row(client):
    applied = apply(client, "Run CS-001")
    answer = applied["answer"]
    assert len(answer["links"]) == len(explain.figures(answer["text"]))
    linked = {link["client"] for link in answer["links"] if link}
    assert linked and linked <= {row["client"] for row in applied["impact"]}


# Fallbacks: no key, eCFR unreachable, a model that changes a figure ---------------------------------------


def test_the_run_works_with_no_key_and_ecfr_unreachable(client):
    events, preview = ask(client, "What if the overtime cap drops to $10,000?")
    by_node = {e["node"]: e for e in nodes(events) if e["state"] not in ("running", "queued")}
    # the code reads a parameter and its value exactly, so this question never needs the model
    assert by_node["intake"]["source"] == "live" and by_node["intake"]["provider"] == "pattern reader"
    assert by_node["law"]["source"] == "cached" and "eCFR API did not answer" in by_node["law"]["fallback"]
    # a phrasing the code cannot read would have gone to the model; with no key it says so
    unread = {e["node"]: e for e in nodes(ask(client, "How would things look if single filers could deduct more overtime?")[0])
              if e["state"] not in ("running", "queued")}
    assert unread["intake"]["source"] == "fallback" and "no Anthropic API key" in unread["intake"]["fallback"]
    assert preview["status"] == "what_if" and preview["hypothetical"] and "preview_token" not in preview
    assert preview["change"]["rule_edits"][0]["cite"].startswith("Hypothetical")
    assert preview["answer"]["source"] == "template"
    health = client.get("/api/health").json()
    assert health["services"]["anthropic"]["state"] == "off" and health["services"]["ecfr"]["state"] == "error"


def _model(monkeypatch, reply):
    monkeypatch.setattr(server, "_provider", lambda: ("anthropic", ""))
    monkeypatch.setattr(llm, "ask", reply)


# Longer than a question, so the model reads it even though the code could read its first sentence.
PASTED_WHAT_IF = ("What if the overtime cap drops to $10,000? We are planning for a possible change and want to see which prepared "
                  "returns in the book it would reach, how far each one would move, and which filed returns would need a closer look.")


def test_answers_are_written_by_code_even_when_a_model_is_on(client, monkeypatch):
    asked = []

    def reply(provider, prompt, **_):
        asked.append(prompt)
        return prompt.split("Note: ", 1)[-1]

    first = apply(client, "Run CS-001")
    _model(monkeypatch, reply)
    events, result = ask(client, "Why did C-031's tax go down?", [first["history_item"]])
    assert result["answer"]["source"] == "template"
    assert result["answer"]["headline"] == "C-031's tax went down $660 in this session, from $7,955 to $7,295."
    node = next(e for e in nodes(events) if e["node"] == "explain" and e["state"] == "done")
    assert node["source"] == "live" and node["provider"] == "code (figures inserted from the run)"
    assert asked == []  # the code read the question exactly and wrote the answer: no model call at all


def test_a_model_proposal_not_in_the_text_falls_back_to_the_parser(client, monkeypatch):
    def reply(provider, prompt, **_):
        if "Classify it" in prompt:
            return json.dumps({"intent": "change", "rule_edits": [{"path": "overtime.cap.other", "to": 9000, "cite": ""}]})
        return prompt.split("Answer: ", 1)[-1]

    _model(monkeypatch, reply)
    events, preview = ask(client, PASTED_WHAT_IF)
    intake = next(e for e in nodes(events) if e["node"] == "intake" and e["state"] == "done")
    assert intake["source"] == "fallback" and "the value 9000 is not in the text" in intake["fallback"]
    assert preview["change"]["rule_edits"][0]["to"] == "10000"


def test_a_model_change_that_fails_validation_gives_way_to_the_code(client, monkeypatch):
    def reply(provider, prompt, **_):
        if "Classify it" in prompt:  # a relative edit copied as words: in the text, but not a value
            return json.dumps({"intent": "change", "rule_edits": [{"path": "overtime.cap.other", "to": "$10,000", "cite": ""}],
                               "what_if": True, "quote": "What if the overtime cap drops to $10,000?"})
        return prompt.split("Note: ", 1)[-1]

    _model(monkeypatch, reply)
    events, preview = ask(client, PASTED_WHAT_IF)
    assert preview["status"] == "what_if" and preview["change"]["rule_edits"][0]["to"] == "10000"
    assert preview["change"]["rule_edits"][0]["cite"].startswith("Hypothetical")


def test_figures_outside_the_run_are_found():
    assert explain.figures_outside("C-017 owes $1,151 more", {"c": "C-017", "v": "1151"}) == []
    assert explain.figures_outside("C-017 owes $1,200 more", {"c": "C-017", "v": "1151"}) == ["1200"]


# Approval ----------------------------------------------------------------------------------------------


def test_a_forged_preview_token_is_not_approved(client):
    _, preview = ask(client, "Run CS-001")
    events, result = decide(client, preview, token="0" * 40)
    assert result["status"] == "stale"
    approve = next(e for e in nodes(events) if e["node"] == "approve" and e["state"] == "failed")
    assert "not the preview the server showed" in approve["summary"]


def test_a_preview_cannot_be_approved_against_another_session(client):
    first = apply(client, "Run CS-001")
    _, preview = ask(client, "set overtime.cap.other to 2500 per P.L. 120-1, test change")
    _, result = decide(client, preview, history=[first["history_item"]])
    assert result["status"] == "stale"


def test_a_history_without_receipts_is_refused(client):
    applied = apply(client, "Run CS-001")
    forged = {**applied["history_item"], "receipt": ""}
    _, result = ask(client, "Why did C-031's tax go down?", [forged])
    assert result["status"] == "error" and "receipt does not match" in result["message"]
    wage_edit = {"change": {"id": "CS-150", "kind": "fact", "title": "raise", "fact_edits": [{"client_id": "C-003", "path": "people[0].wages", "to": 99999}]},
                 "approver": "Someone Else", "materiality": "50", "receipt": applied["history_item"]["receipt"]}
    _, result = ask(client, "Why did C-003's tax go down?", [wage_edit])
    assert result["status"] == "error" and "receipt does not match" in result["message"]


def test_receipts_bind_the_order_of_approvals(client):
    first = apply(client, "Run CS-001")
    second = apply(client, "set overtime.cap.other to 2500 per P.L. 120-1, test change", [first["history_item"]])
    _, result = ask(client, "Why did C-031's tax go down?", [second["history_item"], first["history_item"]])
    assert result["status"] == "error"
    _, result = ask(client, "Why did C-031's tax go down?", [first["history_item"], second["history_item"]])
    assert result["status"] == "answered"


def test_turning_down_a_preview_is_audited_and_applies_nothing(client):
    _, preview = ask(client, "Run CS-001")
    events, result = decide(client, preview, decision="reject", approver="C. Reviewer")
    assert result["status"] == "turned_down" and "history_item" not in result
    assert result["audit"][-1]["outcome"] == "turned down" and result["audit"][-1]["approver"] == "C. Reviewer"
    assert result["answer"]["headline"] == f"{preview['change']['id']} was turned down by C. Reviewer. Nothing changed."
    states = {e["node"]: e["state"] for e in nodes(events) if e["state"] != "running"}
    assert states["notes"] == "skipped" and states["audit"] == "done"


def test_a_blank_approver_is_refused(client):
    _, preview = ask(client, "Run CS-001")
    _, result = decide(client, preview, approver="   ")
    assert result["status"] == "rejected" and result["errors"][0]["field"] == "approver"


# Limits ------------------------------------------------------------------------------------------------


def test_a_document_outside_the_statements_on_file_is_refused(client):
    change = {"id": "CS-002", "kind": "document", "title": "x", "client_id": "C-017", "document": "api key.env"}
    events = stream(client, "/api/run", {"question": "Why did C-017's tax go down?", "history": [{"change": change, "approver": "x", "receipt": "x"}]})
    assert result_of(events)["status"] == "error"
    assert "not a statement on file" in result_of(events)["message"]
    _, preview = ask(client, "Run CS-001")
    events, result = decide(client, {**preview, "change": {**change, "document": "../private/PLAN.md"}})
    assert result["status"] == "rejected" and "not a statement on file" in result["message"]


def test_requests_are_validated_with_field_reasons(client):
    response = client.post("/api/run", json={"history": []})
    assert response.status_code == 422
    assert response.json()["fields"][0]["field"] == "question"
    response = client.post("/api/run", json={"question": "x" * (pipeline.QUESTION_CHARS + 1)})
    assert response.status_code == 422


def test_a_large_body_is_refused(client):
    response = client.post("/api/run", content=b"{" + b" " * (70 * 1024) + b"}", headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_runs_are_throttled_per_address(client, monkeypatch):
    monkeypatch.setattr(server, "RUNS", server.Throttle(2, 600))
    for _ in range(2):
        assert client.post("/api/run", json={"question": "Run CS-900"}).status_code == 200
    response = client.post("/api/run", json={"question": "Run CS-900"})
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


def test_over_the_model_limit_a_run_uses_the_parser(client, monkeypatch):
    _model(monkeypatch, lambda provider, prompt, **_: pytest.fail("the model should not be called"))
    monkeypatch.setattr(server, "LLM_RUNS", server.Throttle(0, 600))
    events, _ = ask(client, PASTED_WHAT_IF)
    intake = next(e for e in nodes(events) if e["node"] == "intake" and e["state"] == "done")
    assert intake["source"] == "fallback" and "rate limit" in intake["fallback"]


def test_the_key_never_reaches_a_response(client, monkeypatch):
    secret = "sk-ant-api03-SECRETSECRETSECRET"
    monkeypatch.setattr(llm, "anthropic_key", lambda: secret)
    monkeypatch.setattr(llm, "check_anthropic", lambda timeout=5.0: ("ok", llm.ANTHROPIC_MODEL))

    def reply(provider, prompt, **_):
        llm._record(provider="anthropic", model=llm.ANTHROPIC_MODEL, ms=5, tokens_in=10, tokens_out=5, max_tokens=500, outcome="end_turn")
        if "Classify it" in prompt:
            return json.dumps({"intent": "change", "rule_edits": [{"path": "overtime.cap.other", "to": 10000, "cite": ""}]})
        return prompt.split("Answer: ", 1)[-1].split("Note: ", 1)[-1]

    monkeypatch.setattr(llm, "ask", reply)
    health = client.get("/api/health")
    events, preview = ask(client, PASTED_WHAT_IF)
    assert preview["status"] == "what_if"
    _, fix = ask(client, "Run CS-001")
    approval = stream(client, "/api/approve", {"change": fix["change"], "history": [], "materiality": "50", "approver": "A", "decision": "approve",
                                              "preview_token": fix["preview_token"]})
    assert secret not in health.text and secret not in json.dumps(events) and secret not in json.dumps(approval)
    intake = next(e for e in nodes(events) if e["node"] == "intake" and e["state"] == "done")
    assert intake["request"]["calls"][0]["tokens_in"] == 10 and "prompt" not in intake["request"]["calls"][0]


# The other endpoints -------------------------------------------------------------------------------------


def test_health_examples_and_book(client):
    health = client.get("/api/health").json()
    assert health["ok"] and health["book"] == {"label": "SYNTHETIC", "tax_year": 2025, "returns": 40}
    assert health["pack"]["version"] == 1 and health["services"]["policyengine"]["release"] == "2.90.4"
    examples = client.get("/api/examples").json()["examples"]
    assert {e["id"] for e in examples} >= {"fix", "document", "what-if", "invalid-900", "invalid-901"}
    book = client.get("/api/book").json()
    assert len(book["returns"]) == 40 and book["statuses"] == {"prep": 14, "review": 12, "filed": 14}
    assert book["parity"]["agree"] == 40 and book["drift"] == {}
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_no_cross_origin_access_is_granted(client):
    response = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_every_example_question_runs(client):
    for example in client.get("/api/examples").json()["examples"]:
        events = stream(client, "/api/run", {"question": example["question"]})
        assert events[-1]["type"] == "run_finished" and events[-1]["status"] != "error", example


def test_the_pipeline_stream_ends_on_an_unexpected_error(monkeypatch):
    monkeypatch.setattr(pipeline, "replay", lambda history: (_ for _ in ()).throw(RuntimeError("boom")))
    events = pipeline.drain(pipeline.question(pipeline.RunRequest(question="Run CS-001"), pipeline.Models(None, "off")))
    assert events[-1] == {**events[-1], "type": "run_finished", "status": "error"}
    assert any(e["type"] == "error" and "RuntimeError" in e["message"] for e in events)


# Review fixes: request origin, model checks, citations, sources ------------------------------------------


def test_a_run_must_be_json_from_this_site(client):
    body = json.dumps({"question": "Run CS-900"})
    assert client.post("/api/run", content=body, headers={"content-type": "text/plain"}).status_code == 415
    response = client.post("/api/run", content=body, headers={"content-type": "application/json", "origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post("/api/run", content=body, headers={"content-type": "application/json", "sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    same = client.post("/api/run", content=body, headers={"content-type": "application/json", "origin": "http://testserver"})
    assert same.status_code == 200


def test_forwarding_headers_count_only_on_the_host(client, monkeypatch):
    monkeypatch.setattr(server, "RUNS", server.Throttle(1, 600))
    assert client.post("/api/run", json={"question": "Run CS-900"}, headers={"x-real-ip": "1.1.1.1"}).status_code == 200
    assert client.post("/api/run", json={"question": "Run CS-900"}, headers={"x-real-ip": "2.2.2.2"}).status_code == 429


def test_a_nested_model_value_is_refused():
    from taxdiff.changes import ChangeSet, FactEdit, RuleEdit
    from taxdiff.lanes import intake

    person = ChangeSet(id="CS-170", kind="fact", title="x", fact_edits=[FactEdit(client_id="C-003", path="people[0]", to={"wages": 999})])
    assert "not a single number" in intake.unsupported(person, "C-003 earns more")[0]
    brackets = ChangeSet(id="CS-171", kind="rule", title="x", rule_edits=[RuleEdit(path="brackets.single", to=[[1, 0.1], [None, 0.2]], cite="x")])
    assert "not a single number" in intake.unsupported(brackets, "new brackets per x")[0]
    wage = ChangeSet(id="CS-172", kind="fact", title="x", fact_edits=[FactEdit(client_id="C-003", path="people[0].wages", to=0.5)])
    assert intake.unsupported(wage, "C-003 wages 50") != []  # a percentage only for a rate


def test_a_citation_must_be_in_the_text_in_words_too():
    from taxdiff.changes import ChangeSet, RuleEdit
    from taxdiff.lanes import intake

    def problems(cite: str, text: str) -> list[str]:
        change = ChangeSet(id="CS-173", kind="rule", title="x", rule_edits=[RuleEdit(path="overtime.cap.other", to=12500, cite=cite)])
        return intake.unsupported(change, text)

    assert problems("Revenue Ruling 2025-69", "Notice 2025-69 sets the cap at $12,500.")
    assert not problems("IRS Notice 2025-69", "Notice 2025-69 sets the cap at $12,500.")


def test_a_rewording_may_not_add_a_magnitude_or_swap_an_id():
    from taxdiff.lanes import notes

    template = "C-003 (in preparation): update the return. Cause: CS-001 Count only the premium. Tax change: +$480."
    assert notes.changed_facts(template, template.replace("+$480", "+$480 million")) == "the rewording added a unit or magnitude"
    assert notes.changed_facts(template, template.replace("CS-001", "C-001")) == "the rewording changed a client or change id"
    assert notes.changed_facts(template, template + " See Revenue Ruling.") == "the rewording added a source"
    assert notes.changed_facts(template, "In preparation, C-003: update the return. Cause: CS-001 Count only the premium. Tax change: +$480.") == ""


def test_the_model_note_never_reaches_the_screen(client, monkeypatch):
    def reply(provider, prompt, **_):
        if "Classify it" in prompt:
            return json.dumps({"intent": "change", "note": "C-999 owes $1,000,000 under CS-999",
                               "rule_edits": [{"path": "overtime.cap.other", "to": 10000, "cite": "", "url": "https://evil.example"}]})
        return prompt.split("Answer: ", 1)[-1]

    _model(monkeypatch, reply)
    events, preview = ask(client, "What if the overtime cap drops to $10,000?")
    assert "C-999" not in json.dumps(events) and "evil.example" not in json.dumps(events)
    assert preview["change"]["rule_edits"][0]["url"] is None


def test_each_changed_line_cites_the_rules_it_read(client):
    _, preview = ask(client, "set senior.amount to 6500 per P.L. 120-1 test A\nset overtime.cap.other to 9000 per P.L. 120-2 test B")
    assert preview["status"] == "waiting"
    cites = {(row["client"], line["line"]): line["citation"]["text"] for row in preview["impact"] for line in row["lines"]}
    assert any(line == "sch1a_overtime" for _, line in cites) and any(line == "sch1a_senior" for _, line in cites)
    for (_, line), text in cites.items():
        if line == "sch1a_overtime":
            assert text == "P.L. 120-2 test B"
        if line == "sch1a_senior":
            assert text == "P.L. 120-1 test A"


def test_an_unexpected_error_is_audited_and_says_no_more_than_its_kind(monkeypatch):
    def broken(history):
        raise RuntimeError("secret file text: sk-ant-api03-XYZ")

    monkeypatch.setattr(pipeline, "replay", broken)
    events = pipeline.drain(pipeline.question(pipeline.RunRequest(question="Run CS-001"), pipeline.Models(None, "off")))
    text = json.dumps(events)
    assert "secret file text" not in text and "sk-ant" not in text
    result = result_of(events)
    assert result["status"] == "error" and result["audit"][0]["outcome"] == "error"
    states = {e["node"]: e["state"] for e in nodes(events) if e["state"] != "running"}
    assert states["question"] == "failed" and states["audit"] == "done"


def test_engine_b_that_finds_no_cached_result_keeps_its_measured_time(client):
    events, _ = ask(client, "set overtime.cap.other to 8800 per Hypothetical check")
    engine_b = next(e for e in nodes(events) if e["node"] == "engine_b" and e["state"] == "skipped")
    assert engine_b["source"] == "cached" and engine_b["duration_ms"] > 0 and "no PolicyEngine-US result" in engine_b["fallback"]


# What-ifs, a full session, and sizes no rule or fact takes (gate 3) -----------------------------------------


def test_a_what_if_is_previewed_and_never_applied(client):
    events, result = ask(client, "What if the overtime cap drops to $10,000?")
    states = {e["node"]: e["state"] for e in nodes(events) if e["state"] not in ("running", "queued")}
    assert result["status"] == "what_if" and events[-1]["status"] == "what_if"
    assert "preview_token" not in result and "pack_after" not in result
    assert states["approve"] == "skipped" and states["notes"] == "skipped" and states["explain"] == "done"
    answer = result["answer"]
    assert result["kpis"]["reached"] > 0 and answer["kind"] == "what_if"
    assert answer["headline"] == "C-012's tax would change by +$240."
    changed = next(section for section in answer["sections"] if section.get("title") == "What it would change")
    rows = {row["label"]: row["value"][0]["text"] for row in changed["rows"]}
    assert rows == {"Overtime cap for single and head of household filers": "$12,500 to $10,000", "Overtime cap for joint filers": "$25,000, unchanged"}
    assert "Nothing was applied" in answer["text"]
    # a decision sent for a what-if is refused, even with a token signed for the same edit previewed as an instruction
    _, normal = ask(client, "set overtime.cap.other to 10000 per P.L. 120-1, test change")
    _, refused = decide(client, {**normal, "hypothetical": True})
    assert refused["status"] == "rejected" and "what-if" in refused["message"]


def test_a_full_session_refuses_another_approval(client, monkeypatch):
    first = apply(client, "Run CS-001")
    monkeypatch.setattr(pipeline, "MAX_HISTORY", 1)
    _, preview = ask(client, "C-017's employer sent a corrected overtime statement", [first["history_item"]])
    assert preview["status"] == "waiting" and preview["history_full"]
    _, refused = decide(client, preview, [first["history_item"]])
    assert refused["status"] == "history_full" and "reset the session" in refused["message"]


@pytest.mark.parametrize(("value", "message"), [
    ("1e1000000000", "under 1,000,000,000,000"),
    ("123456789012345", "under 1,000,000,000,000"),
    ("1e-1000000000", "decimal places"),
    ("0.0000001", "decimal places"),
])
def test_a_number_of_absurd_size_fails_validation_without_being_written_out(client, value, message):
    _, result = ask(client, f"set overtime.cap.other to {value} per Hypothetical change")
    assert result["status"] == "rejected"
    assert [(e["field"], message in e["message"]) for e in result["errors"]] == [("overtime.cap.other", True)]
    assert len(json.dumps(result)) < 200_000


def test_a_fact_of_absurd_size_fails_validation():
    from taxdiff.api.session import base
    from taxdiff.changes import ChangeSet, FactEdit, validate_change

    state = base()
    for value in ("1e1000000000", "1e-1000000000"):
        change = ChangeSet(id="CS-100", kind="fact", title="t", fact_edits=[FactEdit(client_id="C-017", path="people[0].wages", to=value)])
        result = validate_change(change, state.pack, state.book)
        assert not result.ok and result.errors[0].field.startswith("C-017.people.0.wages")


def test_hosted_approvals_need_a_signing_key(client, monkeypatch):
    monkeypatch.setattr(server, "HOSTED", True)
    monkeypatch.setattr(server.signing, "CONFIGURED", False)
    response = client.post("/api/approve", json={"change": {"id": "CS-001", "kind": "rule", "title": "t"}, "decision": "approve",
                                                 "preview_token": "x" * 40})
    assert response.status_code == 503 and "TAXDIFF_SECRET" in response.json()["detail"]


def test_an_answer_is_a_headline_then_sections_built_by_code(client):
    preview = ask(client, "Run CS-001")[1]
    applied = decide(client, preview)[1]["answer"]
    assert applied["headline"] == "CS-001 was applied to 15 of 40 returns: 5 filed returns need amendment review and 2 wait for a document."
    kinds = [section["type"] for section in applied["sections"]]
    assert kinds[0] == "facts" and "list" in kinds
    # totals by direction, never a net across clients; a return waiting on a document shows the range Engine A bounded
    totals = {row["label"]: "".join(part["text"] for part in row["value"]) for row in applied["sections"][0]["rows"]}
    assert totals == {"More tax": "11 returns, +$8,028", "Less tax": "1 return, -$660",
                      "Waiting for a document": "2 returns, +$1,958 to +$3,212 until the documents arrive",
                      "Filed returns owing more": "4 returns, +$2,808 vs. the filed returns",
                      "Filed returns due a refund": "1 return, -$660 vs. the filed returns"}
    titled = {section.get("title"): section for section in applied["sections"]}
    assert [item[0]["text"] for item in titled["Document requests"]["items"]] == ["C-017", "C-019"]
    amend = [(item[0]["text"], item[2]["text"], item[2]["target"]) for item in titled["Amendment review"]["items"] if len(item) > 2]
    assert ("C-017", "+$1,030 to +$1,580", {"client": "C-017", "line": "tax"}) in amend
    assert ("C-031", "-$660", {"client": "C-031", "line": "tax"}) in amend
    # every figure after the totals carries its row, so the screen never parses prose to link a number
    for section in applied["sections"][1:]:
        parts = section.get("content") or [p for row in section.get("rows", []) for p in row["value"]] or [p for item in section.get("items", []) for p in item]
        for part in parts:
            if part["text"].startswith(("+$", "-$")):
                assert part["target"] and part["target"]["client"]


def test_a_statement_that_settles_an_unknown_is_set_against_the_return_as_filed(client):
    first = apply(client, "Run CS-001")
    second = apply(client, "C-017's employer sent a corrected overtime statement", [first["history_item"]])
    answer = second["answer"]
    assert answer["headline"].endswith(": 1 filed return needs amendment review.")
    totals = {row["label"]: "".join(part["text"] for part in row["value"]) for row in answer["sections"][0]["rows"]}
    assert totals == {"Settled by this change": "1 return: C-017", "Filed returns owing more": "1 return, +$1,151 vs. the filed returns"}
    titled = {section.get("title"): section for section in answer["sections"]}
    conflicts = ["".join(part["text"] for part in item) for item in titled["Conflicts with the return as filed"]["items"]]
    assert conflicts == ["C-017: The statement supports $1,920 of qualified overtime under IRS Notice 2025-69; the return as filed deducted $7,500."]
    assert second["kpis"]["tax_change_unknown"] == 0 and second["kpis"]["unknown_moves"] == {"returns": 0, "range": None}


def test_a_statement_that_contradicts_a_filed_return_is_flagged_before_the_rules_are_corrected(client):
    # At rule pack v1 the statement moves no line (all of box 14 counts), yet it shows the return deducted far more
    # than the FLSA premium: the return goes to amendment review with the reason, not to "No action".
    _, preview = ask(client, "C-017's employer sent a corrected overtime statement")
    row = next(item for item in preview["impact"] if item["client"] == "C-017")
    assert row["route"] == "amendment" and not row["lines"]
    assert row["conflict"] == ("The statement supports $1,920 of qualified overtime under IRS Notice 2025-69; the return as filed deducted $7,500. "
                               "Under the Notice's rule its tax would be +$1,151 against the return as filed.")
    # the client's letter says the deduction is too large, never "decreases your tax by $0, so you may be due a refund"
    letter = next(item["text"] for item in preview["letters"] if item["client"] == "C-017" and item["kind"] == "client_notice")
    assert "larger than your employer's statement supports" in letter and "$0" not in letter and "refund" not in letter


def test_a_priced_conflict_follows_the_amend_threshold_and_its_letter_states_the_amount(client):
    # At rules version 1 a $250 premium contradicts C-033's filed deduction; under the Notice it is +$42, under $50.
    _, small = ask(client, "set C-033 people[0].ot_premium_reported to 250")
    row = next(item for item in small["impact"] if item["client"] == "C-033")
    assert row["route"] == "below_materiality" and row["conflict_change"]["text"] == "+$42"
    assert row["route_reason"] == "corrected facts contradict the return as filed"
    assert small["kpis"]["conflict_moves"]["clients"] == ["C-033"] and small["kpis"]["conflict_moves"]["amount"]["text"] == "+$42"
    letter = next(item["text"] for item in small["letters"] if item["client"] == "C-033" and item["kind"] == "client_notice")
    assert "larger than the corrected information supports; correcting it increases your tax by $42" in letter
    assert "do not plan to file one" in letter and "does not apply (IRC 6662(d)(1)(A))" in letter
    # the statement's conflict on C-017 is +$1,151: amendment review, and the letter gives the amount
    _, statement = ask(client, "C-017's employer sent a corrected overtime statement")
    letter = next(item["text"] for item in statement["letters"] if item["client"] == "C-017" and item["kind"] == "client_notice")
    assert "your employer's statement supports; correcting it increases your tax by $1,151" in letter
    assert "Treas. Reg. 1.6664-2(c)" not in letter


def test_a_statement_that_contradicts_a_return_in_progress_sends_it_back_to_the_reviewer(client):
    # C-019 is in review with $9,600 of overtime deducted; a premium of $1,500 contradicts it under any rules.
    _, preview = ask(client, "set C-019 people[0].ot_premium_reported to 1500")
    row = next(item for item in preview["impact"] if item["client"] == "C-019")
    assert row["route"] == "reviewer"
    assert row["conflict"].startswith("The corrected facts support $1,500 of qualified overtime under IRS Notice 2025-69; the prepared return deducts")


def test_questions_about_the_session_are_answered_from_the_approved_changes(client):
    _, empty = ask(client, "Which filed returns need amending?")
    assert empty["answer"]["headline"] == "No filed return needs an amended return in this session."
    first = apply(client, "Run CS-001")
    events, amend = ask(client, "Which filed returns need amending?", [first["history_item"]])
    assert amend["status"] == "answered" and amend["answer"]["headline"] == "5 filed returns need an amended return."
    listed = next(section for section in amend["answer"]["sections"] if section.get("title") == "Amendment review")
    assert [item[0]["text"] for item in listed["items"]] == ["C-017", "C-024", "C-026", "C-029", "C-031"]
    _, waiting = ask(client, "What is waiting on clients?", [first["history_item"]])
    assert waiting["answer"]["headline"] == "2 returns wait for a document."
    assert {e["node"]: e["state"] for e in nodes(events) if e["state"] not in ("running", "queued")}["approve"] == "skipped"


def test_a_preview_gets_a_change_id_the_session_has_not_shown(client):
    first = stream(client, "/api/run", {"question": "What if the overtime cap drops to $10,000?", "history": [], "materiality": "50", "used_ids": 0})
    second = stream(client, "/api/run", {"question": "C-017's wages are $70,000", "history": [], "materiality": "50", "used_ids": 1})
    statement = stream(client, "/api/run", {"question": "C-017's employer sent a corrected overtime statement", "history": [], "materiality": "50", "used_ids": 2})
    assert result_of(first)["change"]["id"] == "CS-100" and result_of(second)["change"]["id"] == "CS-101"
    # a statement on file runs as its saved change set, with the same id as on the command line
    assert result_of(statement)["change"]["id"] == "CS-002"


def _cite_problem(client, question: str, field: str) -> str:
    _, preview = ask(client, question)
    assert preview["status"] == "rejected", question
    return next(error["message"] for error in preview["errors"] if error["field"] == field)


def test_an_authority_must_be_one_that_can_set_the_value(client):
    # IRS guidance applies an amount the Code fixes but cannot change it; only a law amending the Code can.
    assert _cite_problem(client, "set overtime.cap.other to 2500 per Rev. Proc. 2024-40", "overtime.cap.other.cite").startswith(
        "Rev. Proc. 2024-40 cannot set the overtime cap for single and head of household filers: IRC 225 sets it")
    assert _cite_problem(client, "Per Notice 2025-62, set overtime.cap.other to 10000", "overtime.cap.other.cite").startswith(
        "Notice 2025-62 cannot set the overtime cap")
    # the law that enacted the $12,500 cap cannot support a different one
    assert _cite_problem(client, "set overtime.cap.other to 10000 per P.L. 119-21", "overtime.cap.other.cite").startswith(
        "P.L. 119-21 gives the overtime cap for single and head of household filers as $12,500")
    # an authority the rules mark as superseded for the parameter cannot set it
    assert _cite_problem(client, "set standard_deduction.mfj to 30000 per Rev. Proc. 2024-40", "standard_deduction.mfj.cite").startswith(
        "the rules mark Rev. Proc. 2024-40 as superseded for the standard deduction, married filing jointly")
    # a change that restates the current value changes nothing
    assert _cite_problem(client, "set tips.cap to 25000 per P.L. 120-1", "rule_edits") == "nothing to change: the tips cap is already $25,000"
    # a Code section other than the one that holds the amount cannot set it
    assert _cite_problem(client, "set overtime.cap.other to 2500 per IRC 1", "overtime.cap.other.cite").startswith(
        "IRC 1 cannot set the overtime cap for single and head of household filers: IRC 225 sets it")
    # a law can set a statutory amount, and a notice can set what counts as qualified overtime
    assert ask(client, "set overtime.cap.other to 10000 per P.L. 120-1")[1]["status"] == "waiting"
    assert ask(client, "set overtime.definition to flsa_premium per IRS Notice 2025-69")[1]["status"] == "waiting"


def test_a_citation_that_gives_the_current_value_cannot_support_another(client):
    _, preview = ask(client, "set overtime.cap.other to 2500 per IRC 225, a test change")
    assert preview["status"] == "rejected"
    problem = next(error["message"] for error in preview["errors"] if error["field"] == "overtime.cap.other.cite")
    assert problem.startswith("IRC 225 gives the overtime cap for single and head of household filers as $12,500")


def test_a_cited_authority_that_is_not_at_its_address_stops_the_change(client, monkeypatch):
    monkeypatch.setattr(law, "_status", lambda url, timeout: 404 if "rr-25-99" in url else 200)
    _, fake = ask(client, "set overtime.definition to flsa_premium per Rev. Rul. 2025-99")
    assert fake["status"] == "rejected" and "token" not in json.dumps(fake).lower().replace("preview_token", "")
    assert any("Rev. Rul. 2025-99 was not found at https://www.irs.gov/pub/irs-drop/rr-25-99.pdf (HTTP 404)" in error["message"]
               for error in fake["errors"])
    _, real = ask(client, "set standard_deduction.age65.unmarried to 2100 per Rev. Proc. 2025-32")
    check = real["change_view"]["edits"][0]["check"]
    assert real["status"] == "waiting" and check["state"] == "found" and check["url"].endswith("rp-25-32.pdf")


def test_a_result_tells_one_clients_story_in_plain_words(client):
    _, preview = ask(client, "Run CS-001")
    assert preview["story"]["text"] == ("C-026, a joint return already filed. Its overtime deduction would go from $18,000 to $6,000. "
                                        "Its tax would go up $1,440 against the filed return. Next step: amendment review. "
                                        "The client would get a letter that explains it.")
    applied = decide(client, preview)[1]
    assert applied["story"]["text"].startswith("C-026, a joint return already filed. Its overtime deduction goes from $18,000")
