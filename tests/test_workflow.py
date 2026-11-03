"""Change sets end to end: routing, rejection, documents, intake, parity, and the full-recompute check."""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

import pytest

from taxdiff.changes import ChangeSet, FactEdit, RuleEdit, load_change
from taxdiff.facts import load_book
from taxdiff.lanes import intake
from taxdiff.paths import BOOK, CHANGES, RULES
from taxdiff.provenance import Unknown
from taxdiff.rules import SCHEMA, load_pack
from taxdiff.workflow import initial_state, run_change

from .conftest import make_return, person

OVERTIME_CLIENTS = {
    "C-003", "C-005", "C-008", "C-010", "C-012", "C-014", "C-016", "C-017",
    "C-019", "C-021", "C-024", "C-026", "C-029", "C-031", "C-033",
}


@pytest.fixture()
def state():
    return initial_state(load_pack(RULES), load_book(BOOK), "off")


def apply(state, name: str, **kwargs):
    kwargs.setdefault("approve", True)
    return run_change(state, load_change(CHANGES / name), parity_mode="off", **kwargs)


def routes(run) -> dict[str, str]:
    return {item.client_id: item.route for item in run.report.reached}


def test_overtime_fix_reaches_only_overtime_returns(state):
    run = apply(state, "001-overtime-premium.yaml")
    assert run.applied
    assert set(routes(run)) == OVERTIME_CLIENTS
    assert run.report.unexpected == []
    assert len(run.report.not_reached) == 40 - len(OVERTIME_CLIENTS)


def test_overtime_fix_routes(state):
    run = apply(state, "001-overtime-premium.yaml")
    assert run.report.counts() == {"update": 3, "reviewer": 4, "amendment": 5, "document": 1, "below_materiality": 1, "no_change": 1}
    got = routes(run)
    assert got["C-003"] == "update"  # in preparation
    assert got["C-012"] == "reviewer"  # in review
    assert got["C-024"] == "amendment"  # filed
    assert got["C-010"] == "no_change"  # married filing separately: zero before and after
    assert got["C-019"] == "document"  # mixed overtime with no split
    assert got["C-033"] == "below_materiality"  # filed, tax change under $50


def test_filed_refund_opportunity(state):
    run = apply(state, "001-overtime-premium.yaml")
    item = next(i for i in run.report.reached if i.client_id == "C-031")
    assert item.route == "amendment"
    assert item.filed_delta_tax == Decimal("-660.00")


def test_a_return_waiting_on_a_document_shows_the_range_of_its_tax_change(state):
    # Hand-computed from the 2025 Tax Table. C-017: $7,500 of 1.5x overtime pay with no FLSA split, filed with all
    # of it deducted (tax $5,495). The premium is between $0 and $2,500 (a third of the pay): taxable income
    # $55,250 gives tax $7,075, and $52,750 gives $6,525. C-019 (in review, tax $5,003 before): $9,600 of pay,
    # premium $0 to $3,200, taxable $53,250 ($6,635) to $50,050 ($5,931).
    run = apply(state, "001-overtime-premium.yaml", approve=False)
    items = {item.client_id: item for item in run.report.reached}
    assert items["C-017"].delta_tax is None and items["C-017"].needs
    assert items["C-017"].filed_range == (Decimal(1030), Decimal(1580))
    assert items["C-017"].tax_range == (Decimal(1030), Decimal(1580))
    assert items["C-019"].tax_range == (Decimal(928), Decimal(1632)) and items["C-019"].filed_range is None
    assert all(item.tax_range is None for item in items.values() if item.delta_tax is not None)
    assert run.report.unknown_moves() == {"returns": 2, "ranged": 2, "low": Decimal(1958), "high": Decimal(3212)}
    # the statement's amount lands inside the range: C-017 is $1,151 above the return as filed
    second = apply(apply(state, "001-overtime-premium.yaml").state, "002-employer-statement-c017.yaml")
    assert Decimal(1030) <= second.report.reached[0].filed_delta_tax <= Decimal(1580)


def test_a_missing_amount_is_bounded_only_when_its_range_is_known(pack_v2):
    from taxdiff.engine import bounds, compute, extremes

    mixed = make_return(people=[person(wages=70_000, ot_box14=6_000, ot_multiple="2", ot_basis="mixed")])
    assert bounds(extremes(mixed, pack_v2), "sch1a_overtime") == (Decimal(0), Decimal(1500))  # double time: a fourth
    assert isinstance(compute(mixed, pack_v2).values["tax"], Unknown)  # the bounds never become the line's value
    no_multiple = make_return(people=[person(wages=70_000, ot_box14=6_000, ot_basis="mixed")])
    assert extremes(no_multiple, pack_v2) is None
    tips = make_return(people=[person(wages=40_000, tips_qualified=8_000)])
    assert bounds(extremes(tips, pack_v2), "sch1a_tips") == (Decimal(0), Decimal(8000))  # occupation not yet confirmed


def test_notes_carry_the_range_until_the_document_arrives(state):
    from taxdiff.lanes import notes

    run = apply(state, "001-overtime-premium.yaml", approve=False)
    item = next(i for i in run.report.reached if i.client_id == "C-017")
    assert "Against the return as filed: +$1,030 to +$1,580 until the document arrives." in notes.reviewer_note(item, "test")
    letter = notes.client_notice(item, 2025, Decimal(50))
    assert "increases your tax by between $1,030 and $1,580" in letter and "Form 1040-X" in letter


def test_employer_statement_resolves_unknown(state):
    first = apply(state, "001-overtime-premium.yaml")
    second = apply(first.state, "002-employer-statement-c017.yaml")
    assert second.applied
    assert routes(second) == {"C-017": "amendment"}
    item = second.report.reached[0]
    assert item.after.values["sch1a_overtime"] == Decimal("1920.00")
    assert item.filed_delta_tax == Decimal("1151")  # tax $6,646 recomputed against $5,495 filed
    assert second.report.unexpected == []


def test_invalid_rule_change_is_rejected_and_logged(state):
    run = apply(state, "900-invalid-negative-cap.yaml")
    assert not run.applied
    messages = {str(error) for error in run.validation.errors}
    assert "overtime.cap.other: must be zero or more" in messages
    assert "overtime.cap.other.cite: a citation is required" in messages
    assert run.state.pack.version == state.pack.version
    assert run.state.audit[-1].outcome == "failed validation"


def test_statement_for_wrong_year_is_rejected(state):
    run = apply(state, "901-statement-wrong-year.yaml")
    assert not run.applied
    assert any("statement is for 2024" in str(error) for error in run.validation.errors)


def test_a_return_missed_by_the_reads_stops_the_change(state, monkeypatch):
    """If the full recompute finds a return outside the reached list that moved, nothing is applied."""
    import taxdiff.impact as impact

    real = impact.find_reached
    monkeypatch.setattr(impact, "find_reached", lambda touched, results: real(touched, results) - {"C-024"})
    run = apply(state, "001-overtime-premium.yaml")
    assert not run.applied
    assert run.report.unexpected == ["C-024"]
    assert any("full recompute" in str(error) for error in run.validation.errors)
    assert run.state.pack.version == 1
    assert run.state.audit[-1].outcome == "stopped"


def test_cli_previews_unless_approved(capsys):
    import json as _json

    from taxdiff import cli

    cli.main(["--parity", "off", "run", str(CHANGES / "001-overtime-premium.yaml"), "--json"])
    previewed = _json.loads(capsys.readouterr().out)
    cli.main(["--parity", "off", "run", str(CHANGES / "001-overtime-premium.yaml"), "--json", "--approve", "--approver", "test"])
    applied = _json.loads(capsys.readouterr().out)
    assert previewed["runs"][0]["applied"] is False and applied["runs"][0]["applied"] is True
    assert len(previewed["runs"][0]["reached"]) == 15  # a preview shows amounts and routes
    assert [event["outcome"] for event in previewed["audit"]] == ["previewed"]
    assert any(lane["summary"] == "approved by test" for lane in applied["runs"][0]["lanes"])
    assert applied["audit"][-1]["approver"] == "test" and applied["audit"][-1]["outcome"] == "applied"


def test_preview_does_not_apply(state):
    run = apply(state, "001-overtime-premium.yaml", approve=False)
    assert not run.applied
    assert set(routes(run)) == OVERTIME_CLIENTS
    assert run.state.pack.version == 1
    assert run.state.audit[-1].outcome == "previewed"
    # The preview carries the amounts and routes approval would apply.
    assert routes(run)["C-031"] == "amendment" and run.pending.pack.version == 2
    assert run.lane("approve").state == "waiting" and run.lane("notes") is None


def test_approval_is_off_by_default(state):
    assert not run_change(state, load_change(CHANGES / "001-overtime-premium.yaml"), parity_mode="off").applied


def _nudged(kind: str, value):
    """A different valid value of the same kind: money +$1,000, rates halved, rate schedule limits +$1,000,
    dates a day later, and the other overtime definition."""
    if kind == "rate":
        return str(value / 2)
    if kind in ("money", "positive"):
        return str(value + 1000)
    if kind == "brackets":
        return [[None if upper is None else str(upper + 1000), str(rate)] for upper, rate in value]
    if kind == "date":
        return (value + timedelta(days=1)).isoformat()
    return "flsa_premium" if value == "box14_total" else "box14_total"


def test_every_rule_parameter_change_stays_inside_its_reach(state):
    """Change each of the 29 parameters in turn; any return outside the reached set must not move."""
    for key, kind in SCHEMA.items():
        to = _nudged(kind, state.pack.value(key))
        change = ChangeSet(id="CS-500", kind="rule", title=f"nudge {key}", rule_edits=[RuleEdit(path=key, to=to, cite="P.L. 120-1, test nudge")])
        run = run_change(state, change, parity_mode="off", approve=True)
        assert run.applied, key
        assert run.report.unexpected == [], key


def test_every_problem_in_a_rule_change_is_reported(state):
    change = ChangeSet(
        id="CS-501",
        kind="rule",
        title="two problems",
        rule_edits=[RuleEdit(path="no.such.key", to="1", cite="test"), RuleEdit(path="overtime.cap.other", to="-5", cite="")],
    )
    messages = {str(error) for error in run_change(state, change, parity_mode="off").validation.errors}
    assert messages == {
        "no.such.key: unknown parameter",
        "overtime.cap.other: must be zero or more",
        "overtime.cap.other.cite: a citation is required",
    }


def test_hypothetical_cap_after_the_overtime_fix(state):
    first = apply(state, "001-overtime-premium.yaml")
    run = apply(first.state, "003-hypothetical-overtime-cap.yaml")
    assert run.report.counts() == {"reviewer": 2, "amendment": 2, "no_change": 8}
    # C-017 and C-019 still wait for the documents CS-001 asked for; a cap change does not move them, so it gives
    # them no new step and no second request
    assert {cid for cid, route in routes(run).items() if route != "no_change"} == {"C-012", "C-021", "C-024", "C-031"}


def test_overtime_fix_tax_changes(state):
    run = apply(state, "001-overtime-premium.yaml")
    delta = {item.client_id: item.delta_tax for item in run.report.reached}
    assert delta["C-003"] == Decimal("480") and delta["C-033"] == Decimal("48")


def test_random_fact_edits_stay_inside_their_reach(state):
    rng = random.Random(11)
    fields = ["wages", "ot_box14", "ot_premium_reported", "ot_flsa_hours", "ot_regular_rate", "tips_qualified"]
    current = state
    for n in range(60):
        ret = rng.choice(current.book.returns)
        person = rng.randrange(len(ret.people))
        field = rng.choice(fields)
        value = str(rng.randrange(0, 20000, 250))
        change = ChangeSet(
            id="CS-600",
            kind="fact",
            title=f"edit {n}",
            fact_edits=[FactEdit(client_id=ret.client_id, path=f"people[{person}].{field}", to=value)],
        )
        run = run_change(current, change, parity_mode="off", approve=True)
        assert run.applied
        assert run.report.unexpected == [], (ret.client_id, field, value)
        current = run.state


def test_intake_parser():
    rule = intake.parse("set overtime.cap.other to $10,000 per Hypothetical", "CS-101")
    assert rule.kind == "rule" and rule.rule_edits[0].to == "10000" and rule.rule_edits[0].cite == "Hypothetical"
    fact = intake.parse("set C-019 people[0].ot_flsa_hours to 150", "CS-102")
    assert fact.kind == "fact" and fact.fact_edits[0].client_id == "C-019"
    with pytest.raises(ValueError):
        intake.parse("make overtime smaller", "CS-103")


def test_cached_parity_agrees_before_and_after_the_overtime_fix():
    pack, book = load_pack(RULES), load_book(BOOK)
    before = initial_state(pack, book, "cache")
    assert {parity.state for parity in before.parity.values()} == {"agree"}  # all 40 under rule pack v1
    after = run_change(before, load_change(CHANGES / "001-overtime-premium.yaml"), parity_mode="cache", approve=True).state
    states = {cid: parity.state for cid, parity in after.parity.items()}
    assert {cid for cid, s in states.items() if s == "not_checked"} == {"C-017", "C-019"}  # waiting for documents
    assert not [cid for cid, s in states.items() if s == "differs"]
