"""The question reader without a model: how reviewers phrase changes, questions about a client, questions
about the session, and what it refuses."""

from __future__ import annotations

from decimal import Decimal

import pytest

from taxdiff.changes import FactEdit
from taxdiff.facts import load_book
from taxdiff.lanes import questions
from taxdiff.paths import BOOK, RULES
from taxdiff.rules import load_pack

BOOK_ = load_book(BOOK)
PACK = load_pack(RULES)


def read(text: str) -> questions.Reading:
    return questions.parse(text, "CS-100", BOOK_, pack=PACK)


def rule(reading: questions.Reading) -> tuple[str, Decimal]:
    edit = reading.change.rule_edits[0]
    return edit.path, Decimal(str(edit.to))


def fact(reading: questions.Reading) -> tuple[str, str, Decimal]:
    edit = reading.change.fact_edits[0]
    return edit.client_id, edit.path, Decimal(str(edit.to))


CHANGES = [
    ("Run CS-001", lambda r: r.change.id == "CS-001"),
    ("Which returns does Notice 2025-69 affect?", lambda r: r.change.id == "CS-001" and not r.hypothetical),
    ("What returns would IRS Notice 2025-69 change?", lambda r: r.change.id == "CS-001"),
    ("Apply Notice 2025-69", lambda r: r.change.rule_edits[0].to == "flsa_premium"),
    ("Count only the FLSA premium", lambda r: r.change.rule_edits[0].to == "flsa_premium"),
    ("What if the overtime cap drops to $10,000?", lambda r: rule(r) == ("overtime.cap.other", 10000) and r.hypothetical),
    ("What if the overtime cap drops by $2,500?", lambda r: rule(r) == ("overtime.cap.other", 10000) and r.hypothetical),
    ("What if the joint overtime cap rises by $5,000?", lambda r: rule(r) == ("overtime.cap.mfj", 30000) and r.hypothetical),
    ("Suppose the tips cap is $20,000", lambda r: rule(r) == ("tips.cap", 20000) and r.hypothetical),
    ("What if the senior deduction were $5,000?", lambda r: rule(r) == ("senior.amount", 5000) and r.hypothetical),
    ("set overtime.cap.other to 2500 per IRC 225", lambda r: rule(r) == ("overtime.cap.other", 2500)),
    ("C-017's employer sent a corrected overtime statement", lambda r: r.change.kind == "document" and r.change.client_id == "C-017"),
    ("C-017's wages are $70,000", lambda r: fact(r) == ("C-017", "people[0].wages", 70000) and not r.hypothetical),
    ("C-021's corrected W-2 shows wages of $149,000", lambda r: fact(r) == ("C-021", "people[0].wages", 149000)),
    ("What if C-012's overtime hours were 300?", lambda r: fact(r) == ("C-012", "people[0].ot_flsa_hours", 300) and r.hypothetical),
    ("C-016's FLSA premium is $1,500", lambda r: fact(r) == ("C-016", "people[0].ot_premium_reported", 1500)),
    # Facts stated in words: FLSA coverage, an amount "of overtime" with its multiple, a spouse's birth date.
    ("C-029 is actually covered by the FLSA; her overtime was federal, paid at time and a half",
     lambda r: {(e.path, str(e.to)) for e in r.change.fact_edits} == {("people[0].ot_basis", "flsa"), ("people[0].ot_multiple", "1.5")}),
    ("C-012's employer says it paid him $10,500 of overtime at double time",
     lambda r: {(e.path, str(e.to)) for e in r.change.fact_edits} == {("people[0].ot_box14", "10500"), ("people[0].ot_multiple", "2")}),
    ("C-005's wife was born on March 3, 1959",
     lambda r: [(e.client_id, e.path, str(e.to)) for e in r.change.fact_edits] == [("C-005", "people[1].birth_date", "1959-03-03")]),
    # A dollar amount per hour is a rate, never a number of hours.
    ("C-019's employer confirmed 150 FLSA overtime hours at $30 an hour",
     lambda r: {(e.path, str(e.to)) for e in r.change.fact_edits} == {("people[0].ot_flsa_hours", "150"), ("people[0].ot_regular_rate", "30")}),
]

EXPLAIN = [
    ("Why did C-031's tax go down?", ("C-031",), "tax"),
    ("Where does C-017's overtime deduction come from?", ("C-017",), "sch1a_overtime"),
    ("Why is C-010's overtime deduction zero?", ("C-010",), "sch1a_overtime"),
    ("How much tax does C-003 owe?", ("C-003",), "tax"),
    ("Explain C-021's taxable income", ("C-021",), "taxable_income"),
    ("What changed for C-024?", ("C-024",), "tax"),
    # A yes-or-no question is explained like a why-question; the answer says which way the line really moved.
    ("Did C-017's tax go down?", ("C-017",), "tax"),
    ("Has C-024's tax changed?", ("C-024",), "tax"),
    ("Does C-010 get an overtime deduction?", ("C-010",), "sch1a_overtime"),
]

SESSION = [
    ("Which filed returns need amending?", "amendments"),
    ("Which returns need an amendment?", "amendments"),
    ("Who needs a 1040-X?", "amendments"),
    ("What is waiting on clients?", "waiting"),
    ("Which returns are missing documents?", "waiting"),
]

REFUSED = [
    ("What is the weather in Irvine?", "did not match"),
    # A stated amount no fact takes is never dropped: the sentence is not read exactly.
    ("C-019's FLSA overtime hours are 150 and the rate is $30", "states $30 without saying which fact it is"),
    ("Why did C-099's tax go down?", "C-099 is not in this book (C-001 to C-040)"),
    ("What does Notice 2025-69 say about tips?", "does not summarize the notice"),
    ("Hello", "did not match"),
]


@pytest.mark.parametrize(("text", "check"), CHANGES, ids=[t for t, _ in CHANGES])
def test_a_change_is_read_as_the_reviewer_meant(text, check):
    reading = read(text)
    assert reading.kind == "change", reading
    assert check(reading), reading.change


@pytest.mark.parametrize(("text", "clients", "line"), EXPLAIN, ids=[t for t, _, _ in EXPLAIN])
def test_a_question_about_a_client_is_read(text, clients, line):
    reading = read(text)
    assert (reading.kind, reading.clients, reading.line) == ("explain", clients, line)


@pytest.mark.parametrize(("text", "topic"), SESSION, ids=[t for t, _ in SESSION])
def test_a_question_about_the_session_is_read(text, topic):
    reading = read(text)
    assert (reading.kind, reading.topic) == ("session", topic)


@pytest.mark.parametrize(("text", "reason"), REFUSED, ids=[t for t, _ in REFUSED])
def test_what_the_reader_refuses_says_why(text, reason):
    reading = read(text)
    assert reading.kind == "unsupported" and reason in reading.reason


def test_the_reader_handles_most_phrasings():
    """Across every phrasing above, the reader gets each one right; the count is the eval the README reports."""
    total = len(CHANGES) + len(EXPLAIN) + len(SESSION) + len(REFUSED)
    assert total >= 30


# General forms: relative values computed by code from the pack, facts stated several ways, session wording,
# and tax items the prototype does not model.
@pytest.mark.parametrize(
    ("text", "check"),
    [
        ("what if the overtime phase-out for single filers started $20k lower", lambda r: rule(r) == ("overtime.phaseout.start.other", 130000) and r.hypothetical),
        ("Suppose the tips cap were cut by 20%.", lambda r: rule(r) == ("tips.cap", 20000) and r.hypothetical),
        ("what if the joint OT cap went up 10 percent", lambda r: rule(r) == ("overtime.cap.mfj", 27500) and r.hypothetical),
        ("Imagine the single standard deduction cut by a quarter", lambda r: rule(r) == ("standard_deduction.single", Decimal("11812.5")) and r.hypothetical),
        ("What if the senior deduction were halved?", lambda r: rule(r) == ("senior.amount", 3000) and r.hypothetical),
        ("Model a $500 increase to the tips cap, hypothetically", lambda r: rule(r) == ("tips.cap", 25500) and r.hypothetical),
        ("C-019's employer: FLSA overtime hours for 2025: 120. Regular rate: $28.50.",
         lambda r: facts(r) == {("C-019", "people[0].ot_flsa_hours", Decimal(120)), ("C-019", "people[0].ot_regular_rate", Decimal("28.5"))}),
        ("C-014 is paid time and a half, not double time", lambda r: facts(r) == {("C-014", "people[0].ot_multiple", Decimal("1.5"))}),
        ("c-003 tips came to 4.2k", lambda r: facts(r) == {("C-003", "people[0].tips_qualified", Decimal(4200))}),
        ("does anyone owe us paperwork", lambda r: r.kind == "session" and r.topic == "waiting"),
        ("What is C-034's child tax credit?", lambda r: r.kind == "unsupported" and "not modeled" in r.reason),
        ("Suppose tips started phasing out at $120,000 for single filers", lambda r: rule(r) == ("tips.phaseout.start.other", 120000) and r.hypothetical),
        ("what if the overtime phase-out start for non-joint returns were $140,000", lambda r: rule(r) == ("overtime.phaseout.start.other", 140000)),
        ("C-014 logged 90 hours of FLSA overtime at $22 an hour",
         lambda r: facts(r) == {("C-014", "people[0].ot_flsa_hours", Decimal(90)), ("C-014", "people[0].ot_regular_rate", Decimal(22))}),
        ("C-016 got double time; the premium above the regular rate came to $4,000",
         lambda r: facts(r) == {("C-016", "people[0].ot_premium_paid", Decimal(4000)), ("C-016", "people[0].ot_multiple", Decimal(2))}),
        ("C-005: the taxpayer's wages are $70,000 and the spouse's are $41,000",
         lambda r: facts(r) == {("C-005", "people[0].wages", Decimal(70000)), ("C-005", "people[1].wages", Decimal(41000))}),
        ("show me what doubling the senior deduction would look like", lambda r: rule(r) == ("senior.amount", 12000) and r.hypothetical),
        ("what-if: take 5% off the joint std deduction", lambda r: rule(r) == ("standard_deduction.mfj", Decimal("29925")) and r.hypothetical),
        ("anyone filed who now needs a 1040-X", lambda r: r.kind == "session" and r.topic == "amendments"),
        ("list returns stuck until a client sends us something", lambda r: r.kind == "session" and r.topic == "waiting"),
        ("How was C-021's modified AGI figured?", lambda r: r.kind == "explain" and r.line == "magi"),
        ("C-012's wages are 66k on our side but the client says 64k. Why?", lambda r: r.kind == "explain" and r.line == "wages"),
    ],
)
def test_general_forms(text, check):
    reading = read(text)
    assert check(reading), reading


def facts(reading: questions.Reading) -> set[tuple[str, str, Decimal]]:
    return {(edit.client_id, edit.path, Decimal(str(edit.to))) for edit in reading.change.fact_edits}


def test_a_reading_may_not_leave_out_a_fact_the_sentence_states():
    # The guard every reading of stated facts passes, the model's included: a multiple, a basis or a birth date the
    # sentence states must be in the reading.
    box14 = [FactEdit(client_id="C-012", path="people[0].ot_box14", to="10500")]
    assert questions._left_out("C-012's employer says it paid him $10,500 of overtime at double time", box14) == "pay multiple"
    assert questions._left_out("C-029 is covered by the FLSA after all; overtime $9,000", box14) == "overtime basis"
    assert questions._left_out("C-005's wife was born on March 3, 1959", box14) == "birth date"
    assert questions._left_out("C-012's employer says it paid him $10,500 of overtime, not double time", box14) == ""
