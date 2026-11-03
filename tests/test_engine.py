"""Golden and boundary cases. Expected values are worked by hand from the 2025 rate schedules and the
Schedule 1-A rules, and match PolicyEngine-US 2.90.4 where it models the same line."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from taxdiff.engine import compute
from taxdiff.provenance import Unknown
from taxdiff.tax import bracket_tax, form_tax, tax_table_row

from .conftest import make_return, person

D = Decimal


def test_rate_schedule_and_tax_table_differ_at_60000(pack_v1):
    brackets = pack_v1.value("brackets.single")
    assert bracket_tax(brackets, D(60000)) == D("8114.00")
    assert tax_table_row(D(60000)) == (D(60000), D(60050))
    assert form_tax(brackets, D(60000), D(100000)) == D(8120)


def test_tax_table_bottom_rows(pack_v1):
    brackets = pack_v1.value("brackets.single")
    assert [form_tax(brackets, D(ti), D(100000)) for ti in (3, 10, 20, 30)] == [D(0), D(1), D(2), D(4)]


def test_worksheet_at_and_above_100000_keeps_cents(pack_v1):
    brackets = pack_v1.value("brackets.single")
    assert form_tax(brackets, D("130249"), D(100000)) == D("24106.76")


# Published 2025 Tax Table cells and Tax Computation Worksheet results (research/params-ty2025.md, B6).
@pytest.mark.parametrize(
    ("status", "taxable", "expected"),
    [
        ("single", "60000", "8120"),
        ("mfj", "60000", "6726"),
        ("hoh", "60000", "6863"),
        ("mfs", "60000", "8120"),
        ("mfj", "25300", "2562"),
        ("single", "3000", "303"),
        ("single", "48450", "5579"),
        ("single", "48500", "5590"),
        ("single", "99999", "16909"),
        ("mfj", "99999", "11823"),
        ("hoh", "99999", "15170"),
        ("single", "100000", "16914.00"),
        ("mfj", "100000", "11828.00"),
        ("hoh", "100000", "15175.00"),
        ("single", "150000", "28847.00"),
        ("single", "300000", "74547.25"),
        ("mfj", "800000", "220062.50"),
    ],
)
def test_published_tax_values(pack_v1, status, taxable, expected):
    assert form_tax(pack_v1.value(f"brackets.{status}"), D(taxable), D(100000)) == D(expected)


def test_single_with_reported_premium(pack_v2):
    ret = make_return(people=[person(wages=60_000, ot_premium_reported=3_000)])
    out = compute(ret, pack_v2).values
    assert out["sch1a_overtime"] == D(3000)
    assert out["taxable_income"] == D(41250)
    assert out["tax_formula"] == D("4711.50")


@pytest.mark.parametrize(("magi", "expected"), [(150_999, D(5000)), (151_000, D(4900)), (151_999, D(4900))])
def test_overtime_phaseout_counts_whole_thousands(pack_v2, magi, expected):
    ret = make_return(people=[person(wages=magi, ot_premium_reported=5_000)])
    assert compute(ret, pack_v2).values["sch1a_overtime"] == expected


def test_joint_seniors_each_reduced(pack_v2):
    ret = make_return(
        "mfj",
        [person(born=date(1955, 3, 1), wages=160_000), person("spouse", born=date(1957, 8, 9), wages=0)],
    )
    out = compute(ret, pack_v2).values
    assert out["standard_deduction"] == D(34700)
    assert out["sch1a_senior"] == D(10800)
    assert out["taxable_income"] == D(114500)
    assert out["tax_formula"] == D("15018.00")


@pytest.mark.parametrize(("born", "eligible"), [(date(1961, 1, 1), True), (date(1961, 1, 2), False)])
def test_age_65_boundary(pack_v2, born, eligible):
    out = compute(make_return(people=[person(born=born, wages=50_000)]), pack_v2).values
    assert out["sch1a_senior"] == (D(6000) if eligible else D(0))
    assert out["standard_deduction"] == (D(17750) if eligible else D(15750))


def test_separate_return_gets_no_overtime(pack_v2):
    ret = make_return("mfs", [person(wages=80_000, ot_premium_reported=4_000)])
    out = compute(ret, pack_v2).values
    assert out["sch1a_overtime"] == D(0)
    assert out["taxable_income"] == D(64250)
    assert out["tax_formula"] == D("9049.00")


def test_tips_need_listed_occupation(pack_v2):
    listed = make_return(people=[person(wages=52_000, tips_qualified=12_000, tips_occupation_listed=True)])
    unlisted = make_return(people=[person(wages=52_000, tips_qualified=12_000, tips_occupation_listed=False)])
    assert compute(listed, pack_v2).values["sch1a_tips"] == D(12000)
    assert compute(listed, pack_v2).values["tax_formula"] == D("2671.50")
    assert compute(unlisted, pack_v2).values["sch1a_tips"] == D(0)


def test_head_of_household(pack_v2):
    ret = make_return("hoh", [person(wages=70_000, ot_premium_reported=2_000)])
    out = compute(ret, pack_v2).values
    assert out["standard_deduction"] == D(23625)
    assert out["tax_formula"] == D("4985.00")


@pytest.mark.parametrize(
    ("evidence", "premium"),
    [
        ({"ot_box14": 9_000, "ot_multiple": "1.5", "ot_basis": "flsa"}, D(3000)),
        ({"ot_box14": 4_000, "ot_multiple": "2", "ot_basis": "flsa"}, D(1000)),
        ({"ot_flsa_hours": 120, "ot_regular_rate": 25}, D(1500)),
        ({"ot_box14": 6_000, "ot_multiple": "1.5", "ot_basis": "state_or_contract"}, D(0)),
    ],
)
def test_notice_2025_69_premium_rules(pack_v2, evidence, premium):
    out = compute(make_return(people=[person(wages=70_000, **evidence)]), pack_v2).values
    assert out["sch1a_overtime"] == premium


def test_mixed_overtime_without_split_is_unknown(pack_v2):
    ret = make_return(people=[person(wages=70_000, ot_box14=7_500, ot_multiple="1.5", ot_basis="mixed")])
    result = compute(ret, pack_v2)
    assert isinstance(result.values["sch1a_overtime"], Unknown)
    assert isinstance(result.values["tax"], Unknown)
    assert result.needs and "pay stubs" in result.needs[0]


@pytest.mark.parametrize(
    ("people", "status", "taxable", "tax"),
    [
        # E2E-01: single, born 1959, AGI 80,000 with 3,000 qualified overtime
        ([person(born=date(1959, 5, 5), wages=80_000, ot_premium_reported=3_000)], "single", "53550", "6701"),
        # E2E-03: MFS, under 65, AGI 70,000 including 5,000 qualified overtime
        ([person(wages=70_000, ot_premium_reported=5_000)], "mfs", "54250", "6855"),
        # E2E-04a / 04b: born Jan 1 vs Jan 2, 1961, AGI 70,000
        ([person(born=date(1961, 1, 1), wages=70_000)], "single", "46250", "5315"),
        ([person(born=date(1961, 1, 2), wages=70_000)], "single", "54250", "6855"),
    ],
)
def test_end_to_end_returns(pack_v2, people, status, taxable, tax):
    out = compute(make_return(status, people), pack_v2).values
    assert out["taxable_income"] == D(taxable)
    assert out["tax"] == D(tax)


@pytest.mark.parametrize(("magi", "expected"), [(75_000, "6000"), (75_001, "5999.94"), (100_000, "4500.00"), (175_000, "0")])
def test_senior_phaseout_single_cents_mode(pack_v2, magi, expected):
    out = compute(make_return(people=[person(born=date(1955, 1, 1), wages=magi)]), pack_v2).values
    assert out["sch1a_senior"] == D(expected)


def test_senior_phaseout_joint_each_spouse_cents_mode(pack_v2):
    ret = make_return(
        "mfj", [person(born=date(1955, 1, 1), wages=249_999), person("spouse", born=date(1956, 1, 1), wages=0)]
    )
    assert compute(ret, pack_v2).values["sch1a_senior"] == D("0.12")


def test_pack_v1_counts_all_box14_pay(pack_v1):
    ret = make_return(people=[person(wages=70_000, ot_box14=9_000, ot_multiple="1.5", ot_basis="flsa")])
    assert compute(ret, pack_v1).values["sch1a_overtime"] == D(9000)


def test_overtime_cap(pack_v1):
    ret = make_return(people=[person(wages=90_000, ot_box14=20_000, ot_multiple="1.5", ot_basis="flsa")])
    assert compute(ret, pack_v1).values["sch1a_overtime"] == D(12500)


def test_lines_record_their_reads(pack_v2):
    ret = make_return(people=[person(wages=70_000, ot_box14=9_000, ot_multiple="1.5", ot_basis="flsa")])
    reads = compute(ret, pack_v2).reads
    assert "rule:overtime.definition" in reads["sch1a_overtime"]
    assert "code:overtime.flsa_premium@1" in reads["sch1a_overtime"]
    assert "fact:people[0].ot_premium_reported" in reads["sch1a_overtime"]
    assert "rule:overtime.definition" in reads["tax"]
    assert "rule:overtime.definition" not in reads["standard_deduction"]


def test_no_overtime_means_no_overtime_reads(pack_v2):
    reads = compute(make_return(), pack_v2).all_reads
    assert "rule:overtime.definition" not in reads
    assert "fact:people[0].ot_box14" in reads


@pytest.mark.parametrize(("paid", "multiple", "expected"), [(10_000, "2", "5000.00"), (3_000, "1.5", "3000")])
def test_an_overtime_premium_paid_above_the_regular_rate_follows_method_c(pack_v2, paid, multiple, expected):
    # Notice 2025-69 example 3: $10,000 of premium paid at two times the regular rate; $5,000 qualifies (method C).
    # At time and a half the premium paid above the regular rate is the FLSA premium itself (method A).
    ret = make_return(people=[person(wages=90_000, ot_premium_paid=paid, ot_multiple=multiple)])
    result = compute(ret, pack_v2)
    assert result.values["sch1a_overtime"] == Decimal(expected)
    assert "Method C" in result.work["sch1a_overtime"][0][1] if multiple == "2" else "Method A" in result.work["sch1a_overtime"][0][1]
