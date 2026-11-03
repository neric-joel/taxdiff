"""Notes lane: the reviewer note and the client document request for each routed return.

Every figure in a note is inserted by code from the computed diff. An optional LLM may reword a note; the
rewording is kept only if it carries exactly the same figures, otherwise the template text stands.
"""

from __future__ import annotations

import re
from decimal import Decimal

from ..engine import LINES
from ..impact import ReturnImpact
from ..provenance import Unknown, Value
from ..router import ROUTES
from . import llm

STATUS = {"prep": "in preparation", "review": "in review", "filed": "filed"}


def money(value: Value | None) -> str:
    if value is None or isinstance(value, Unknown):
        return "unknown"
    if value.is_finite() and value != 0 and not -12 <= value.adjusted() <= 15:
        return f"${value:.3E}"  # validation refuses such values; this keeps a stray one from expanding
    return f"${value:,.2f}" if value != value.to_integral_value() else f"${value:,.0f}"


def signed(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}{money(abs(value))}"


def span(pair: tuple[Decimal, Decimal]) -> str:
    """A range of tax changes: "+$1,030 to +$1,580"."""
    return f"{signed(pair[0])} to {signed(pair[1])}"


def reviewer_note(item: ReturnImpact, cause: str) -> str:
    lines = [
        f"{item.client_id} ({STATUS[item.status]}): {ROUTES[item.route].lower()}.",
        f"Cause: {cause}.",
    ]
    for diff in item.diffs:
        if diff.line in ("magi", "tax_formula"):
            continue
        lines.append(f"{LINES[diff.line]}: {money(diff.before)} to {money(diff.after)}.")
    if item.delta_tax is not None:
        lines.append(f"Tax change: {signed(item.delta_tax)}.")
    elif item.tax_range is not None:
        lines.append(f"Tax change: {span(item.tax_range)} until the document arrives.")
    if item.status == "filed" and item.filed_delta_tax is not None:
        lines.append(f"Against the return as filed: {signed(item.filed_delta_tax)}.")
    elif item.status == "filed" and item.filed_range is not None:
        lines.append(f"Against the return as filed: {span(item.filed_range)} until the document arrives.")
    return " ".join(lines)


def _client_words(need: str) -> str:
    """A missing fact as a client would ask for it: no statute names, no internal field names."""
    if need.startswith("FLSA overtime premium"):
        whose = "your spouse's" if "spouse" in need else "your"
        return f"pay stubs or a statement from {whose} employer that shows the overtime premium (the extra half of the pay rate for overtime hours)"
    if "tipped occupation" in need:
        whose = "your spouse's" if "spouse" in need else "your"
        return f"a description of {whose} job from the employer, to confirm it is an occupation that receives tips"
    return need


# An individual's understatement is substantial only above the greater of $5,000 or 10% of the correct tax
# (IRC 6662(d)(1)(A)), so a smaller increase cannot draw that penalty.
SUBSTANTIAL = Decimal(5000)


def _consequences(more: bool | None, tax_year: int, increase: Decimal | None = None) -> str:
    """What the error means for the client, as Circular 230 section 10.21 asks: interest on more tax, the penalty
    only where the amount could draw it, the time limit on a refund. `increase` is the most the tax can go up, when
    known."""
    due = f"April 15, {tax_year + 1}"
    interest = f"If more tax is due, interest runs on it from {due}, the original due date, until it is paid (IRC 6601)"
    if increase is not None and increase < SUBSTANTIAL:
        owe = f"{interest}; at this amount the penalty for a substantial understatement of tax does not apply (IRC 6662(d)(1)(A))."
    else:
        owe = (f"{interest}, and the IRS may add an accuracy-related penalty (IRC 6662); an amended return filed before the IRS "
               "contacts you about the return can keep that penalty from applying (Treas. Reg. 1.6664-2(c)).")
    refund = ("You can claim the refund on Form 1040-X within 3 years after the date you filed the return or 2 years after you paid "
              "the tax, whichever is later (IRC 6511(a)).")
    return owe if more else refund if more is False else f"{owe} {refund}"


def client_notice(item: ReturnImpact, tax_year: int, materiality: Decimal) -> str:
    """The letter that tells a client about an error found on a filed return, whatever its size (Circular 230
    section 10.21 has no minimum), with what it means for them: an amended return is recommended, or, under the
    firm's materiality, offered."""
    delta = item.filed_delta_tax
    more: bool | None = None if delta is None else delta > 0
    if delta is None and item.filed_range is not None:
        more = True if item.filed_range[0] >= 0 else False if item.filed_range[1] <= 0 else None
    increase = delta if delta is not None and delta > 0 else item.filed_range[1] if item.filed_range is not None and more else None
    if item.conflict:
        # A statement that supports less than the return deducted: the tax goes up once the deduction is corrected,
        # whatever the change under the current rules (it may be $0 while those rules still count all overtime pay).
        # When the correction is priced, the letter says by how much.
        priced = item.conflict_delta
        source = "your employer's statement" if item.conflict.startswith("The statement") else "the corrected information"
        if priced is not None and priced > 0:
            found = (f"that the overtime deduction on the return is larger than {source} supports; correcting it "
                     f"increases your tax by {money(priced)}")
            more, increase = True, priced
        else:
            found = (f"that the overtime deduction on the return is larger than {source} supports; we are working out "
                     "how much the correction changes your tax")
            more, increase = True, None
    elif delta is None and item.filed_range is not None and item.filed_range[0] >= 0:
        found = (f"an error in the overtime deduction that increases your tax by between {money(item.filed_range[0])} and "
                 f"{money(item.filed_range[1])}; the exact amount depends on the document we have asked you for")
    elif delta is None and item.filed_range is not None and item.filed_range[1] <= 0:
        found = (f"an error in the overtime deduction that decreases your tax by between {money(-item.filed_range[1])} and "
                 f"{money(-item.filed_range[0])}; the exact amount depends on the document we have asked you for")
    elif delta is None:
        found = "an error in the overtime deduction; the amount depends on the document we have asked you for"
    elif delta > 0:
        found = f"an error in the overtime deduction that increases your tax by {money(delta)}"
    else:
        found = f"an error in the overtime deduction that decreases your tax by {money(-delta)}, so you may be due a refund"
    if item.route == "below_materiality":
        plan = (f"The difference is under the {money(materiality)} for which this firm recommends an amended return, so we do not "
                "plan to file one unless you ask us to.")
    else:
        plan = "We recommend an amended return (Form 1040-X) and will contact you to prepare it."
    return (f"Notice for {item.client_id}: on your {tax_year} return as filed we found {found}. {plan} {_consequences(more, tax_year, increase)} "
            "Please contact us with any questions.")


def letters(item: ReturnImpact, tax_year: int, materiality: Decimal) -> list[tuple[str, str]]:
    """The letters a reached return gets, as (kind, text): a notice for an error on a filed return, and a request
    for the document a line now waits on. Letters are drafted by code and never reworded."""
    out: list[tuple[str, str]] = []
    if item.status == "filed" and item.route in ("amendment", "below_materiality"):
        out.append(("client_notice", client_notice(item, tax_year, materiality)))
    if item.waiting:
        out.append(("document_request", document_request(item, tax_year)))
    return out


def document_request(item: ReturnImpact, tax_year: int) -> str:
    needs = "; and ".join(_client_words(need) for need in item.needs) or "the missing information"
    purpose = (
        "We need it to check the overtime deduction on the return you filed."
        if item.status == "filed"
        else "We need it to finish the overtime deduction on your return."
    )
    return f"Request for {item.client_id}: please send {needs}, for tax year {tax_year}. {purpose}"


_FIGURE = re.compile(r"([+-]?)\$?(\d[\d,]*(?:\.\d+)?)")


def figures(text: str) -> list[str]:
    """Every number in the text in order of appearance, with its sign, without thousands commas or trailing
    zero cents.

    Comparing ordered, signed lists catches a rewording that swaps a before and after value or turns a tax
    increase (+$480) into a refund (-$480).
    """
    found = []
    for sign, digits in _FIGURE.findall(text):
        value = digits.replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        found.append(sign + value)
    return found


_ID = re.compile(r"\bCS?-\d{3}\b")
_MAGNITUDE = re.compile(r"\b(thousand|million|billion|trillion|percent|k|mn|bn)\b|%", re.I)
_AUTHORITY = re.compile(r"\b(notice|ruling|revenue|procedure|publication|regulation|treas|irc|cfr)\b", re.I)


def changed_facts(template: str, reply: str) -> str:
    """Why a rewording does not say what the template says, or "" when it keeps every fact.

    Kept: the figures in order with their signs; the client and change ids in order, so a cause CS-001
    cannot become a client C-001; no magnitude word the template lacks ("$480" cannot become "$480
    million"); and no authority word the template lacks, so a source cannot be added.
    """
    if figures(reply) != figures(template):
        return "the rewording changed a figure"
    if _ID.findall(reply) != _ID.findall(template):
        return "the rewording changed a client or change id"
    if len(_MAGNITUDE.findall(reply)) > len(_MAGNITUDE.findall(template)):
        return "the rewording added a unit or magnitude"
    words = [w.lower() for w in _AUTHORITY.findall(reply)]
    have = [w.lower() for w in _AUTHORITY.findall(template)]
    if any(words.count(w) > have.count(w) for w in set(words)):
        return "the rewording added a source"
    return ""


POLISH_PROMPT = """Rewrite this note for a tax reviewer as two or three plain sentences.
Keep every number, dollar amount, client id and change id exactly as written and in the same order.{keep}
Do not add any number, fact or advice. Return only the rewritten note.

Note: {note}"""


def polish(note: str, client_id: str, provider: str, *, route: str = "") -> tuple[str, str]:
    """Reword an internal reviewer note. Returns (text, source).

    The first sentence, the decision ("C-024 (filed): amendment review."), stays as the template wrote it; only
    the rest is reworded, and the rewording is kept only when its figures equal the rest's figures in the same
    order and signs. Client document requests are never passed here.
    """
    head, sep, rest = note.partition(" Cause: ")
    if not sep or client_id not in head or (route and route.lower() not in head.lower()):
        return note, "template (the note has no decision sentence to keep)"
    rest = "Cause: " + rest
    try:
        reply = llm.ask(provider, POLISH_PROMPT.format(note=rest, keep=""), max_tokens=600).strip()
    except llm.Unavailable as exc:
        return note, f"template ({exc})"
    except Exception as exc:  # an optional rewording never stops an approved change
        return note, f"template (the provider failed: {type(exc).__name__})"
    if reply and not changed_facts(rest, reply):
        return f"{head} {reply}", "LLM wording, figures checked"
    return note, "template (the rewording changed a figure)"
