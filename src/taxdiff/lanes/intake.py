"""Intake lane: turn a written change into a proposed change set.

The pattern parser understands "set <path> to <value> [per <citation>]", optionally with a client id for
a fact. An LLM provider (see llm.py) can read free text instead. Its proposal is checked against the text
it read, then goes through the same validation, preview and approval as any other change.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ..changes import ChangeSet, FactEdit, RuleEdit
from ..rules import SCHEMA, bounded
from . import llm

_SET = re.compile(
    r"^\s*set\s+(?:(?P<client>C-\d{3})\s+)?(?P<path>[a-z0-9_.\[\]]+)\s+to\s+(?P<value>.+?)"
    r"(?:\s+(?:per|citing|cite)\s+(?P<cite>.+))?\s*$",
    re.I,
)


def _value(raw: str) -> Any:
    text = raw.strip().strip("\"'")
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none", "empty"):
        return None
    number = text.replace("$", "").replace(",", "")
    if re.fullmatch(r"-?\d+(\.\d+)?", number):
        return number
    return text


def parse(text: str, change_id: str) -> ChangeSet:
    """Parse one instruction per line. Raises ValueError with the line that could not be read."""
    lines = [part.strip() for part in text.splitlines() if part.strip()]
    if not lines:
        raise ValueError("there is no instruction to read")
    rule_edits: list[RuleEdit] = []
    fact_edits: list[FactEdit] = []
    for line in lines:
        match = _SET.match(line)
        if not match:
            raise ValueError(f'cannot read "{line}". Write: set <path> to <value> per <citation>')
        path, value = match["path"], _value(match["value"])
        if match["client"]:
            fact_edits.append(FactEdit(client_id=match["client"].upper(), path=path, to=value))
        elif path in SCHEMA:
            rule_edits.append(RuleEdit(path=path, to=value, cite=(match["cite"] or "").strip()))
        else:
            raise ValueError(f"{path} is not a rule parameter; name a client (C-001) to change a fact")
    if rule_edits and fact_edits:
        raise ValueError("write rule changes and fact changes as separate change sets")
    kind = "rule" if rule_edits else "fact"
    return ChangeSet(id=change_id, kind=kind, title=lines[0][:120], rule_edits=rule_edits, fact_edits=fact_edits)


PROMPT = """Convert the tax change below into JSON for a change set. Use only these rule parameter paths:
{paths}
Fact paths look like people[0].wages or people[0].ot_premium_reported, with a client id such as C-017.
Return JSON only: {{"rule_edits": [{{"path": "...", "to": <value>, "cite": "<source named in the text>"}}],
"fact_edits": [{{"client_id": "C-000", "path": "people[0].<field>", "to": <value>}}]}}.
Copy values exactly from the text. If the text does not state a value or a source, leave the list empty.

Change: {text}"""


_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _canon(raw: str) -> str:
    """A number written one way: no thousands commas, no leading zeros, no trailing zero decimals."""
    whole, _, fraction = raw.replace(",", "").partition(".")
    whole, fraction = whole.lstrip("0") or "0", fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


_THOUSANDS = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand)\b", re.I)


def numbers(text: str) -> set[str]:
    """Every number written in the text, unsigned and in canonical form; "15.4k" is also 15400, and an amount
    written in words ("twelve thousand five hundred") is also its number."""
    found = {_canon(raw) for raw in _DIGITS.findall(text)}
    found |= {_canon(format(Decimal(raw.replace(",", "")) * 1000, "f")) for raw, _ in _THOUSANDS.findall(text)}
    from .questions import words_value  # a late import: questions imports this module

    spelled = words_value(text)
    if spelled:
        found.add(_canon(str(spelled)))
    return found


def _as_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _out_of_range(number: Decimal) -> bool:
    """True for a size or precision no rule or fact accepts; such a number is never written out in full."""
    try:
        bounded(number)
    except ValueError:
        return True
    return False


def _written(number: Decimal, rate: bool) -> set[str]:
    """How a proposed number can appear in the text: as itself, or, for a rate parameter, as a percentage."""
    forms = {_canon(format(abs(number), "f"))}
    if rate and abs(number) < 1:
        forms.add(_canon(format(abs(number) * 100, "f")))
    return forms


# Words a citation may carry that the text need not repeat: "IRS Notice 2025-69" holds for "Notice 2025-69".
_GENERIC = {"irs", "the", "and", "for", "per", "sec", "section", "under", "see", "u.s.", "us"}


def _cite_problem(cite: str, flat: str, found: set[str]) -> bool:
    """True when a citation is not grounded in the text: it must appear in it, or carry only numbers and
    words (of three letters or more, besides a few generic ones) that appear in it."""
    cite = " ".join(cite.lower().split())
    if not cite or cite in flat:
        return False
    words = {w for w in re.findall(r"[a-z][a-z.]{2,}", cite) if w.strip(".") not in _GENERIC}
    text_words = set(re.findall(r"[a-z][a-z.]{2,}", flat)) | {w.strip(".") for w in re.findall(r"[a-z][a-z.]{2,}", flat)}
    return not (numbers(cite) and numbers(cite) <= found and all(w in text_words or w.strip(".") in text_words for w in words))


def unsupported(change: ChangeSet, text: str) -> list[str]:
    """What an LLM proposal states that the text it read does not. Empty when everything checks.

    Every proposed value must be one number or one word: a list or a group of fields (a rate schedule, a
    whole person) carries numbers this check cannot hold to the text, so it is refused. Every number must
    be written in the text (a rate may be written as a percentage), every client must be named in it, and
    every citation must appear in it or carry only numbers and words that appear in it, so "IRS Notice
    2025-69" passes for text that says "Notice 2025-69" and "Revenue Ruling 2025-69" does not. Values that
    are not numbers, such as a definition name or a date, are left to the approver.
    """
    found, flat = numbers(text), " ".join(text.lower().split())
    problems = []
    for edit in change.rule_edits:
        if isinstance(edit.to, (list, dict, tuple)):
            problems.append(f"{edit.path}: the value is not a single number or word, so it cannot be checked against the text")
            continue
        number = _as_number(edit.to)
        if number is not None and _out_of_range(number):
            problems.append(f"{edit.path}: the value is outside the range this tool accepts")
            continue
        if number is not None and not _written(number, SCHEMA.get(edit.path) == "rate") & found:
            problems.append(f"{edit.path}: the value {edit.to} is not in the text")
        if _cite_problem(edit.cite, flat, found):
            problems.append(f'{edit.path}: the citation "{edit.cite}" is not in the text')
    for edit in change.fact_edits:
        if edit.client_id.lower() not in flat:
            problems.append(f"{edit.client_id}: the client is not named in the text")
        if isinstance(edit.to, (list, dict, tuple)):
            problems.append(f"{edit.client_id} {edit.path}: the value is not a single number or word, so it cannot be checked against the text")
            continue
        number = _as_number(edit.to)
        if number is not None and _out_of_range(number):
            problems.append(f"{edit.client_id} {edit.path}: the value is outside the range this tool accepts")
            continue
        if number is not None and not _written(number, False) & found:
            problems.append(f"{edit.client_id} {edit.path}: the value {edit.to} is not in the text")
    return problems


def propose(text: str, change_id: str, provider: str = "pattern") -> ChangeSet:
    """Propose a change set from text. Raises ValueError (unreadable or unsupported) or llm.Unavailable."""
    if not text.strip():
        raise ValueError("there is no instruction to read")
    if provider == "pattern":
        return parse(text, change_id)
    reply = llm.ask(provider, PROMPT.format(paths=", ".join(SCHEMA), text=text), json_output=True)
    match = re.search(r"\{.*\}", reply, re.S)  # from the first "{" to the last "}": drops prose and code fences
    if not match:
        raise ValueError("the model did not return JSON")
    try:
        data = json.loads(match.group(0))
        # A link is never taken from a model: a citation's URL comes from the rule pack or a person.
        rule_edits = [RuleEdit(**{**edit, "url": None}) for edit in data.get("rule_edits") or []]
        fact_edits = [FactEdit(**edit) for edit in data.get("fact_edits") or []]
    except (ValueError, TypeError, AttributeError) as exc:  # bad JSON, or JSON that is not a list of edits
        raise ValueError("the model's reply is not a change set") from exc
    if not rule_edits and not fact_edits:
        raise ValueError("the model found no change it could state from the text")
    if rule_edits and fact_edits:
        raise ValueError("the model proposed rule and fact changes together; split the text")
    change = ChangeSet(
        id=change_id,
        kind="rule" if rule_edits else "fact",
        title=text.strip()[:120],
        author=f"proposed by {llm.PROVIDERS[provider]}, pending approval",
        rationale="numbers and citations were found in the text; the approver confirms the parameter, any value that is not a number, and the source",
        rule_edits=rule_edits,
        fact_edits=fact_edits,
    )
    problems = unsupported(change, text)
    if problems:
        raise ValueError("the model's proposal does not match the text: " + "; ".join(problems))
    return change
