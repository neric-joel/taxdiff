"""Regular income tax: the rate schedule, the IRS Tax Table method, and the Tax Computation Worksheet."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

Brackets = tuple[tuple[Decimal | None, Decimal], ...]


def bracket_tax(brackets: Brackets, taxable_income: Decimal) -> Decimal:
    """Tax from the rate schedule, to the cent."""
    tax, lower = Decimal(0), Decimal(0)
    for upper, rate in brackets:
        if upper is None or taxable_income <= upper:
            tax += (taxable_income - lower) * rate
            break
        tax += (upper - lower) * rate
        lower = upper
    return tax.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def tax_table_row(taxable_income: Decimal) -> tuple[Decimal, Decimal]:
    """The Tax Table row [at least, but less than] that holds this taxable income.

    Rows are $0-5, $5-15 and $15-25, then $25 wide up to $3,000, then $50 wide up to $100,000.
    """
    if taxable_income < 5:
        return Decimal(0), Decimal(5)
    if taxable_income < 15:
        return Decimal(5), Decimal(15)
    if taxable_income < 25:
        return Decimal(15), Decimal(25)
    width = Decimal(25) if taxable_income < 3000 else Decimal(50)
    low = (taxable_income / width).to_integral_value(rounding=ROUND_FLOOR) * width
    return low, low + width


def form_tax(brackets: Brackets, taxable_income: Decimal, table_below: Decimal) -> Decimal:
    """The tax on Form 1040 line 16.

    Under the Tax Table limit, the published table holds the rate-schedule tax at the row midpoint,
    rounded to the dollar with 50 cents rounding up (this reproduces all 8,248 published cells).
    At or above it, the Tax Computation Worksheet gives the rate-schedule tax. The engine carries cents
    throughout; the instructions allow cents or whole dollars if one choice is used for every amount.
    """
    if taxable_income <= 0:
        return Decimal(0)
    if taxable_income < table_below:
        low, high = tax_table_row(taxable_income)
        return bracket_tax(brackets, (low + high) / 2).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return bracket_tax(brackets, taxable_income)
