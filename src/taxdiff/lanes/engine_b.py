"""Engine B lane: arithmetic parity with PolicyEngine-US, a separate open-source implementation.

PolicyEngine-US takes the qualified overtime premium as an input. The adapter derives it from the evidence
with the overtime definition the rule pack selects, using Engine A's own provision code, so the premium is
not checked independently; tests pin it. Everything after it (caps, phase-outs, deductions, taxable
income, tax) is compared. Agreement shows both engines do the same arithmetic on the same facts and rules;
it does not show the rules are right.

The engine runs three ways. "api" sends the whole book, with any reform, to the public PolicyEngine API
(api.policyengine.org, no key) in one request; the hosted server runs this way, so each run is checked by
the current PolicyEngine-US release. "live" runs the installed policyengine-us library and refreshes the
cache. "cache" reads results saved from an earlier live run, keyed by each return's inputs; it is the
fallback when the API does not answer in time.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import threading
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from ..engine import ComputedReturn
from ..facts import Book, ClientReturn
from ..paths import PARITY_CACHE, RULES, write_text_atomic
from ..provenance import Tracker, Unknown
from ..provisions import OVERTIME
from ..rules import RulePack, load_pack

YEAR = 2025
TOLERANCE = Decimal("0.50")  # PolicyEngine uses float32; the spike measured differences under $0.01

COMPARED: dict[str, str] = {
    "standard_deduction": "standard_deduction",
    "sch1a_tips": "tip_income_deduction",
    "sch1a_overtime": "overtime_income_deduction",
    "sch1a_senior": "additional_senior_deduction",
    "taxable_income": "taxable_income",
    "tax_formula": "income_tax_before_credits",
}

FILING = {"single": "SINGLE", "mfj": "JOINT", "mfs": "SEPARATE", "hoh": "HEAD_OF_HOUSEHOLD"}

_OT, _TIPS = "gov.irs.deductions.overtime_income", "gov.irs.deductions.tip_income"
PARAMETERS: dict[str, tuple[str, ...]] = {
    "overtime.cap.other": (f"{_OT}.cap.SINGLE", f"{_OT}.cap.HEAD_OF_HOUSEHOLD"),
    "overtime.cap.mfj": (f"{_OT}.cap.JOINT",),
    "overtime.phaseout.start.other": (f"{_OT}.phase_out.start.SINGLE", f"{_OT}.phase_out.start.HEAD_OF_HOUSEHOLD"),
    "overtime.phaseout.start.mfj": (f"{_OT}.phase_out.start.JOINT",),
    "tips.cap": (f"{_TIPS}.cap",),
    "tips.phaseout.start.other": (f"{_TIPS}.phase_out.start.SINGLE", f"{_TIPS}.phase_out.start.HEAD_OF_HOUSEHOLD"),
    "tips.phaseout.start.mfj": (f"{_TIPS}.phase_out.start.JOINT",),
    "senior.amount": ("gov.irs.deductions.senior_deduction.amount",),
}


@dataclass(frozen=True)
class Parity:
    state: Literal["agree", "differs", "not_checked"]
    reason: str = ""
    diffs: tuple[tuple[str, Decimal, Decimal], ...] = ()
    authority: str = ""  # for a difference with a known cause: the authority this project's engine follows


@dataclass
class LaneOutput:
    results: dict[str, dict[str, float]] = field(default_factory=dict)  # client_id -> PolicyEngine outputs
    skipped: dict[str, str] = field(default_factory=dict)  # client_id -> reason not checked
    source: str = "cache"
    note: str = ""
    version: str = ""  # the policyengine-us release the results came from
    reused: bool = False  # the API's answer to the same request was reused from memory, not asked again
    fallback: str = ""  # why the API did not answer, when cached results stand in for it


def unavailable(book: Book, reason: str) -> LaneOutput:
    """Every return marked not checked, for a lane that could not run. Routing then goes by status alone."""
    return LaneOutput(skipped={ret.client_id: reason for ret in book.returns}, source="failed", note=reason)


def _age(born: Any, cutoff: Any) -> int:
    age = YEAR - born.year
    return max(age, 65) if born < cutoff else max(age, 18)


def return_inputs(ret: ClientReturn, pack: RulePack) -> dict[str, Any] | None:
    """PolicyEngine inputs for one return, or None when a fact needed for the premium is Unknown.

    The premium comes from the overtime definition the pack selects, so both engines read the law the same
    way and a difference can only come from the arithmetic after the premium.
    """
    people = []
    for i, person in enumerate(ret.people):
        data: dict[str, Any] = {
            "age": _age(person.birth_date, pack.value("age65.born_before")),
            "employment_income": float(person.wages),
        }
        if any(v is not None for v in (person.ot_box14, person.ot_premium_reported, person.ot_flsa_hours)):
            premium = OVERTIME[pack.value("overtime.definition")].compute(Tracker(pack), i, person)
            if isinstance(premium, Unknown):
                return None
            data["fsla_overtime_premium"] = float(premium)
        if person.tips_qualified:
            data["tip_income"] = float(person.tips_qualified)
            data["tip_income_deduction_occupation_requirement_met"] = bool(person.tips_occupation_listed)
        people.append(data)
    return {"filing_status": FILING[ret.filing_status], "people": people}


@lru_cache(maxsize=1)
def baseline_pack() -> RulePack:
    """Rule pack v1 holds the values PolicyEngine-US holds for every mapped parameter; reforms are
    differences from it."""
    return load_pack(RULES)


def reform_for(pack: RulePack, baseline: RulePack) -> tuple[dict[str, float], list[str]]:
    """PolicyEngine parameter overrides for this pack, and the changed parameters it cannot express."""
    reform: dict[str, float] = {}
    unmapped: list[str] = []
    for key, parameter in pack.parameters.items():
        if key == "overtime.definition" or parameter.value == baseline.value(key):
            continue
        if key in PARAMETERS:
            for path in PARAMETERS[key]:
                reform[path] = float(parameter.value)
        else:
            unmapped.append(key)
    return reform, unmapped


ADAPTER_VERSION = 3  # bump when return_inputs or COMPARED changes, so old cached results stop matching


def cache_key(inputs: dict[str, Any], reform: dict[str, float]) -> str:
    payload = json.dumps({"tax_year": YEAR, "adapter": ADAPTER_VERSION, "inputs": inputs, "reform": reform}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def load_cache(path: Path = PARITY_CACHE) -> dict[str, Any]:
    """The cached results. A missing or unreadable cache reads as empty, with the problem noted."""
    if not path.is_file():
        return {"results": {}, "runs": []}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cache.get("results"), dict) or not isinstance(cache.get("runs"), list):
            raise ValueError("unexpected layout")
    except (OSError, ValueError, AttributeError) as exc:
        return {"results": {}, "runs": [], "problem": f"the PolicyEngine cache could not be read ({type(exc).__name__})"}
    return cache


def save_cache(cache: dict[str, Any], path: Path = PARITY_CACHE) -> None:
    write_text_atomic(path, json.dumps(cache, indent=1, sort_keys=True) + "\n")


def situation_for(units: dict[str, dict[str, Any]], *, ask: bool = False) -> dict[str, dict[str, Any]]:
    """One PolicyEngine situation holding every unit as its own tax unit and household. With ask=True each
    tax unit also names the compared variables with no value, which is how the API is asked for them."""
    year = str(YEAR)
    situation: dict[str, dict[str, Any]] = {
        key: {} for key in ("people", "tax_units", "families", "spm_units", "households", "marital_units")
    }
    for key, unit in units.items():
        ids = [f"{key}_{n}" for n in range(len(unit["people"]))]
        for pid, person in zip(ids, unit["people"], strict=True):
            situation["people"][pid] = {name: {year: value} for name, value in person.items()}
        situation["tax_units"][key] = {"members": ids, "filing_status": {year: unit["filing_status"]}}
        if ask:
            situation["tax_units"][key].update({name: {year: None} for name in COMPARED.values()})
        situation["families"][key] = {"members": ids}
        situation["spm_units"][key] = {"members": ids}
        situation["households"][key] = {"members": ids, "state_name": {year: "TX"}}
        if unit["filing_status"] == "JOINT":
            situation["marital_units"][key] = {"members": ids}
        else:
            for pid in ids:
                situation["marital_units"][pid] = {"members": [pid]}
    return situation


API_URL = os.environ.get("TAXDIFF_POLICYENGINE_API", "https://api.policyengine.org/us/calculate")
API_TIMEOUT = float(os.environ.get("TAXDIFF_POLICYENGINE_TIMEOUT", "5"))  # seconds before the cache stands in; a preview never waits longer
_ANSWERS: OrderedDict[str, dict[str, dict[str, float]]] = OrderedDict()  # request digest -> results, per process
_ANSWERS_LOCK = threading.Lock()
_CALL = threading.local()  # whether this thread's last run_api call was answered from memory, so the lane can say so


def _post(body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def run_api(units: dict[str, dict[str, Any]], reform: dict[str, float], timeout: float = API_TIMEOUT) -> dict[str, dict[str, float]]:
    """Compute every unit in one request to the PolicyEngine API. The same request asked again (a replayed
    session) is answered from memory. Raises OSError or ValueError when the API cannot answer."""
    body: dict[str, Any] = {"household": situation_for(units, ask=True)}
    if reform:
        body["policy"] = {path: {f"{YEAR}-01-01.2100-12-31": value} for path, value in reform.items()}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with _ANSWERS_LOCK:
        known = _ANSWERS.get(digest)
    _CALL.reused = known is not None
    if known is not None:
        return known
    data = _post(body, timeout)
    if data.get("status") != "ok" or not isinstance(data.get("result"), dict):
        raise ValueError(f"the PolicyEngine API answered: {str(data.get('message') or data.get('status'))[:160]}")
    year = str(YEAR)
    tax_units = data["result"]["tax_units"]
    results = {key: {name: float(tax_units[key][name][year]) for name in COMPARED.values()} for key in units}
    with _ANSWERS_LOCK:
        _ANSWERS[digest] = results
        while len(_ANSWERS) > 64:
            _ANSWERS.popitem(last=False)
    return results


def run_policyengine(units: dict[str, dict[str, Any]], reform: dict[str, float]) -> dict[str, dict[str, float]]:
    """Compute every unit in one PolicyEngine simulation. Needs the "parity" extra installed."""
    from policyengine_us import (
        Simulation,  # imported here: the import alone takes over a minute
    )

    situation = situation_for(units)
    order = list(units)
    if reform:
        from policyengine_core.reforms import Reform

        span = f"{YEAR}-01-01.2100-12-31"
        policy = Reform.from_dict({path: {span: value} for path, value in reform.items()}, country_id="us")
        simulation = Simulation(situation=situation, reform=policy)
    else:
        simulation = Simulation(situation=situation)
    columns = {name: simulation.calculate(name, YEAR).tolist() for name in COMPARED.values()}
    return {key: {name: float(values[n]) for name, values in columns.items()} for n, key in enumerate(order)}


def installed_version() -> str:
    """The installed policyengine-us release, or "" when it is not installed."""
    try:
        return importlib.metadata.version("policyengine-us")
    except importlib.metadata.PackageNotFoundError:
        return ""


def run_lane(book: Book, pack: RulePack, mode: Literal["cache", "live", "api", "off"] = "cache") -> LaneOutput:
    """PolicyEngine results for every checkable return under this pack.

    "api" asks the public PolicyEngine API and falls back to the cache, with the reason, when it does not
    answer. "cache" reads cached results only. "live" recomputes every checkable return with PolicyEngine-US,
    even those already cached, and writes the results back. "off" checks nothing. A cache holds results from
    one policyengine-us release: a live run under another release starts the cache again.
    """
    output = LaneOutput(source=mode)
    if mode == "off":
        output.skipped = {ret.client_id: "parity turned off" for ret in book.returns}
        return output
    reform, unmapped = reform_for(pack, baseline_pack())
    cache = load_cache()
    output.version = cache.get("policyengine_us", "")
    output.note = cache.get("problem", "")
    keys: dict[str, str] = {}
    checkable: dict[str, dict[str, Any]] = {}
    for ret in book.returns:
        if not all(person.valid_ssn for person in ret.people):
            output.skipped[ret.client_id] = "SSN eligibility is not passed to PolicyEngine-US"
            continue
        inputs = return_inputs(ret, pack)
        if inputs is None:
            output.skipped[ret.client_id] = "a fact is Unknown"
            continue
        key = cache_key(inputs, reform)
        keys[ret.client_id] = key
        checkable[key] = inputs
    missing = "no PolicyEngine result cached for these inputs"
    if checkable and mode == "api":
        started = perf_counter()
        try:
            computed = run_api(checkable, reform)
            reused = bool(getattr(_CALL, "reused", False))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("the PolicyEngine API") else (
                f"the PolicyEngine API did not answer within {API_TIMEOUT:.0f} s ({type(exc).__name__})")
            output.source, output.fallback = "cache", f"{reason}; results saved from policyengine-us {output.version or 'an earlier run'}"
        else:
            seconds = round(perf_counter() - started, 2)
            output.source, output.version = "api", "PolicyEngine API"
            output.note = ("reused the PolicyEngine API's answer to the same request, computed earlier on this server" if reused
                           else f"computed {len(computed)} returns through the PolicyEngine API in {seconds} s")
            output.reused = reused
            for client_id, key in keys.items():
                output.results[client_id] = computed[key]
            if unmapped:
                output.note += "; not passed to PolicyEngine-US, so returns that read them are not checked: " + ", ".join(unmapped)
            return output
    if checkable and mode == "live":
        version = installed_version()
        if not version:
            # A live run was asked for and cached results would not answer it, so nothing is checked.
            missing = output.note = "PolicyEngine-US is not installed here (pip install .[parity])"
            cache = {"results": {}, "runs": []}
        else:
            if cache.get("policyengine_us") != version:
                cache = {"policyengine_us": version, "results": {}, "runs": cache["runs"]}
            started = perf_counter()
            computed = run_policyengine(checkable, reform)
            seconds = round(perf_counter() - started, 1)
            cache["results"].update({key: dict(values, policyengine_us=version) for key, values in computed.items()})
            cache["runs"].append(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "returns": len(computed),
                    "seconds": seconds,
                    "policyengine_us": version,
                    "adapter": ADAPTER_VERSION,
                    "reform": reform,
                }
            )
            cache.pop("problem", None)
            save_cache(cache)
            output.version = version
            output.note = f"computed {len(computed)} returns live in {seconds} s"
    for client_id, key in keys.items():
        if key in cache["results"]:
            output.results[client_id] = cache["results"][key]
        else:
            output.skipped[client_id] = missing
    if unmapped:
        output.note = "; ".join(filter(None, [output.note, "not passed to PolicyEngine-US, so returns that read them are not checked: " + ", ".join(unmapped)]))
    return output


def compare(result: ComputedReturn, outputs: dict[str, float] | None, skipped: str | None = None) -> Parity:
    if skipped is not None or outputs is None:
        return Parity("not_checked", skipped or "no PolicyEngine result")
    diffs = []
    for line, variable in COMPARED.items():
        ours = result.values[line]
        if isinstance(ours, Unknown):
            return Parity("not_checked", "a line is Unknown")
        theirs = Decimal(str(round(outputs[variable], 2)))
        if abs(ours - theirs) > TOLERANCE:
            diffs.append((line, ours, theirs))
    if not diffs:
        return Parity("agree")
    reason, authority = why_differs(result, diffs)
    return Parity("differs", reason, tuple(diffs), authority)


_JOINT_ONLY = {"sch1a_overtime": "IRC 225(e)", "sch1a_tips": "IRC 224(f)", "sch1a_senior": "Schedule 1-A (Form 1040) 2025, part V"}
_STEPS = {"sch1a_overtime": ("18", "Schedule 1-A (Form 1040) 2025, line 19"), "sch1a_tips": ("10", "Schedule 1-A (Form 1040) 2025, line 11")}


def why_differs(result: ComputedReturn, diffs: tuple[tuple[str, Decimal, Decimal], ...] | list[tuple[str, Decimal, Decimal]]) -> tuple[str, str]:
    """The known cause of a difference, from the return and the line, with the authority this project's engine
    follows; or a plain note when no known cause fits. Either way the return goes back to a reviewer."""
    for line, ours, theirs in diffs:
        if line in _JOINT_ONLY and result.filing_status == "mfs" and ours == 0 and theirs > 0:
            return (f"PolicyEngine-US allows this deduction on a separate return; {_JOINT_ONLY[line]} requires a joint return, "
                    "so this project's engine allows none", _JOINT_ONLY[line])
        if line in _STEPS and ours > theirs:
            excess_ref, authority = _STEPS[line]
            excess = next((value for ref, _, value in result.work.get(line, ()) if ref == excess_ref), None)
            if isinstance(excess, Decimal) and excess > 0 and excess % 1000:
                return (f"{authority} counts whole $1,000 steps of modified AGI over the phase-out start, rounded down; "
                        "PolicyEngine-US reduces the deduction continuously", authority)
    return "the engines differ on these lines and no known cause fits; a reviewer checks the return", ""


def parity_for(results: dict[str, ComputedReturn], lane: LaneOutput, unmapped_reads: dict[str, str]) -> dict[str, Parity]:
    """Merge Engine A results with the lane output.

    unmapped_reads marks returns that read a changed parameter PolicyEngine-US was not given; they are not
    checked.
    """
    parity = {}
    for client_id, result in results.items():
        if client_id in unmapped_reads:
            parity[client_id] = Parity("not_checked", unmapped_reads[client_id])
        else:
            parity[client_id] = compare(result, lane.results.get(client_id), lane.skipped.get(client_id))
    return parity
