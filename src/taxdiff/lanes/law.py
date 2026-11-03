"""Law lane: attach the authorities behind each changed rule and each changed line.

The citation list is kept in the repository. Regulation excerpts come from cache/law/ecfr.json, built by
scripts/refresh_law.py from saved eCFR XML. By default the lane makes no network call. With live=True it
fetches the FLSA sections from the eCFR API (falling back to the saved excerpts), checks that each cited
source still answers at its address (irs.gov, Cornell LII), and searches the Federal Register API for
Treasury and IRS documents on the provisions the change touches, newest first. Every live call has a
timeout; a failure is reported with its reason and never stops the change.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from html import unescape
from time import perf_counter

from ..paths import ROOT
from ..rules import authorities as named_authorities

ECFR_CACHE = ROOT / "cache" / "law" / "ecfr.json"
ECFR_API = "https://www.ecfr.gov/api/versioner/v1"
ECFR_TIMEOUT = float(os.environ.get("TAXDIFF_ECFR_TIMEOUT", "3"))  # seconds per request
FETCH_MEMO = 3600.0  # seconds a server keeps the FLSA sections it fetched live (eCFR updates them rarely)


@dataclass(frozen=True)
class Authority:
    key: str
    title: str
    url: str
    scope: str
    excerpt: str = ""
    as_of: str = ""
    fetched: bool = False  # the excerpt was fetched from the eCFR API in this call
    request: str = ""  # the eCFR API request that supplied it, with status, size and time
    fallback: str = ""  # why a live fetch did not supply the excerpt
    checked: str = ""  # what a live request to the source's address found, for this call


STATIC: tuple[Authority, ...] = (
    Authority(
        "notice-2025-69",
        "IRS Notice 2025-69 (Nov 21 2025)",
        "https://www.irs.gov/pub/irs-drop/n-25-69.pdf",
        "How to figure 2025 qualified tips and overtime without separate employer reporting",
    ),
    Authority(
        "irc-225",
        "IRC 225, qualified overtime compensation (added by P.L. 119-21)",
        "https://www.law.cornell.edu/uscode/text/26/225",
        "Overtime deduction: definition, cap, phase-out",
    ),
    Authority(
        "irc-224",
        "IRC 224, qualified tips (added by P.L. 119-21)",
        "https://www.law.cornell.edu/uscode/text/26/224",
        "Tips deduction: definition, cap, phase-out",
    ),
    Authority(
        "irc-151",
        "IRC 151(d)(5)(C), enhanced deduction for seniors",
        "https://www.law.cornell.edu/uscode/text/26/151",
        "Senior deduction: amount and phase-out",
    ),
    Authority(
        "sch1a",
        "Schedule 1-A (Form 1040) 2025",
        "https://www.irs.gov/pub/irs-pdf/f1040s1a.pdf",
        "Where the tips, overtime and senior deductions are figured",
    ),
    Authority(
        "rp-2024-40",
        "Rev. Proc. 2024-40",
        "https://www.irs.gov/pub/irs-drop/rp-24-40.pdf",
        "2025 rate schedules and inflation adjustments",
    ),
    Authority(
        "i1040",
        "2025 Form 1040 instructions",
        "https://www.irs.gov/instructions/i1040gi",
        "Standard deduction, Tax Table and Tax Computation Worksheet",
    ),
    Authority(
        "p501",
        "Publication 501",
        "https://www.irs.gov/publications/p501",
        "Age 65 rule for the standard deduction",
    ),
)

FLSA = ("29 CFR 778.107", "29 CFR 778.110")

FEDERAL_REGISTER = "https://www.federalregister.gov/api/v1/documents.json"
# The Federal Register search for each provision: an exact phrase, Treasury and IRS documents only.
GUIDANCE: dict[str, tuple[str, str]] = {
    "overtime.": ("qualified overtime", "Qualified overtime"),
    "tips.": ("qualified tips", "Qualified tips"),
    "senior.": ("senior deduction", "Senior deduction"),
}

BY_PREFIX: dict[str, tuple[str, ...]] = {
    "overtime.": ("irc-225", "notice-2025-69", "sch1a", *FLSA),
    "tips.": ("irc-224", "notice-2025-69", "sch1a"),
    "senior.": ("irc-151", "sch1a"),
    "standard_deduction.": ("i1040", "rp-2024-40"),
    "age65.": ("p501",),
    "brackets.": ("rp-2024-40",),
    "tax_table.": ("i1040",),
}

BY_LINE: dict[str, tuple[str, ...]] = {
    "sch1a_overtime": BY_PREFIX["overtime."],
    "sch1a_tips": BY_PREFIX["tips."],
    "sch1a_senior": BY_PREFIX["senior."],
    "standard_deduction": BY_PREFIX["standard_deduction."],
    "tax": ("i1040", "rp-2024-40"),
    "tax_formula": ("rp-2024-40",),
}


def _excerpts() -> tuple[dict[str, dict[str, str]], str]:
    """The cached FLSA sections and their as-of date. A missing or unreadable cache gives none."""
    try:
        cache = json.loads(ECFR_CACHE.read_text(encoding="utf-8"))
        sections = {}
        for key in FLSA:
            section = cache["sections"].get(key)
            if section:
                sections[key] = {name: str(section[name]) for name in ("heading", "url", "excerpt")}
        return sections, str(cache.get("as_of", ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, ""


@lru_cache(maxsize=1)
def authorities() -> dict[str, Authority]:
    """Static citations plus the cached FLSA excerpts. Without the cache the FLSA entries carry no excerpt."""
    found = {item.key: item for item in STATIC}
    sections, as_of = _excerpts()
    for key in FLSA:
        section = sections.get(key)
        if section:
            found[key] = Authority(key, f"{key}, {section['heading'].split(' ', 2)[-1]}", section["url"],
                                   "FLSA overtime: context for the premium the FLSA requires",
                                   section["excerpt"], as_of)
        else:
            found[key] = Authority(key, key, f"https://www.ecfr.gov/current/title-29/section-{key.split()[-1]}",
                                   "FLSA overtime: context for the premium the FLSA requires")
    return found


def _text_of(xml: str, tag: str) -> list[str]:
    """The text of each element with this tag, tags removed and spaces collapsed (as refresh_law.py does)."""
    found = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
    return [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", item))).strip() for item in found]


def _get(url: str, timeout: float) -> tuple[bytes, str]:
    """GET one eCFR API URL. Returns the body and a one-line description of the request."""
    started = perf_counter()
    request = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "taxdiff (synthetic case study)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        status = response.status
    return body, f"GET {url}: HTTP {status}, {len(body):,} bytes, {(perf_counter() - started) * 1000:.0f} ms"


_as_of: tuple[float, str] | None = None  # (when it was read, the date title 29 is up to date as of)


def _title_date(timeout: float) -> tuple[str, list[str]]:
    """The date eCFR's title 29 is up to date as of, from the API's title list, remembered for an hour."""
    global _as_of
    if _as_of is not None and perf_counter() - _as_of[0] < 3600:
        return _as_of[1], []
    titles, line = _get(f"{ECFR_API}/titles.json", timeout)
    as_of = next(str(t["up_to_date_as_of"]) for t in json.loads(titles)["titles"] if t.get("number") == 29)
    date.fromisoformat(as_of)  # a reply that is not a date stops here, before it reaches a URL
    _as_of = (perf_counter(), as_of)
    return as_of, [line]


def fetch_flsa(timeout: float = ECFR_TIMEOUT) -> tuple[dict[str, Authority], list[str]]:
    """The FLSA sections as the eCFR API serves them now, and the requests made.

    The date comes from the API's own title list (the day title 29 is up to date as of), then both sections
    are fetched side by side. Raises OSError or ValueError when any request fails or a reply cannot be read.
    """
    as_of, first = _title_date(timeout)

    def section(key: str) -> tuple[str, Authority, str]:
        number = key.split()[-1]
        body, line = _get(f"{ECFR_API}/full/{as_of}/title-29.xml?part=778&section={number}", timeout)
        xml = body.decode("utf-8")
        heading, excerpt = _text_of(xml, "HEAD")[0], _text_of(xml, "P")[0]
        return key, Authority(key, f"{key}, {heading.split(' ', 2)[-1]}", f"https://www.ecfr.gov/current/title-29/section-{number}",
                              "FLSA overtime: context for the premium the FLSA requires", excerpt, as_of, True, line), line

    with ThreadPoolExecutor(max_workers=len(FLSA)) as pool:
        found = list(pool.map(section, FLSA))
    return {key: authority for key, authority, _ in found}, first + [line for _, _, line in found]


_fetched: tuple[float, dict[str, Authority]] | None = None  # (when, the sections) from the last live fetch


def _with_live(keys: list[str]) -> dict[str, Authority]:
    """The authority table with the FLSA entries fetched live (kept for an hour on this server, so one slow
    answer does not hold every run); on any failure, the saved entries with the reason."""
    global _fetched
    table = dict(authorities())
    if not any(key in FLSA for key in keys):
        return table
    if _fetched is not None and perf_counter() - _fetched[0] < FETCH_MEMO:
        minutes = int((perf_counter() - _fetched[0]) // 60)
        note = f"fetched live from the eCFR API {minutes} min ago on this server" if minutes else "fetched live from the eCFR API on this server"
        table.update({key: replace(item, request=note) for key, item in _fetched[1].items()})
        return table
    try:
        live, _ = fetch_flsa()
    except (OSError, ValueError, KeyError, IndexError, StopIteration, UnicodeDecodeError) as exc:
        reason = f"the eCFR API did not answer ({type(exc).__name__}: {exc})"[:300]
        return {key: replace(item, fallback=reason) if key in FLSA else item for key, item in table.items()}
    _fetched = (perf_counter(), live)
    table.update(live)
    return table


def _head(url: str, timeout: float) -> str:
    """A HEAD request to a cited source: what it found, in a few words. Raises OSError when it fails."""
    started = perf_counter()
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "taxdiff (synthetic case study)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        kind = (response.headers.get("Content-Type") or "").split(";")[0].strip()
        changed = response.headers.get("Last-Modified") or ""
    shown = {"application/pdf": "PDF", "text/html": "web page"}.get(kind, kind or "a document")
    when = ""
    if changed:
        try:
            from email.utils import parsedate_to_datetime

            when = f", last changed {parsedate_to_datetime(changed).date().isoformat()}"
        except (TypeError, ValueError):
            when = ""
    return f"found at its address ({shown}{when}; {(perf_counter() - started) * 1000:.0f} ms)"


def authority_url(name: str) -> str | None:
    """Where an authority (in the normal form of rules.authorities) is published, when its address follows a
    pattern: IRS notices, revenue procedures and rulings from 2020 on at irs.gov/pub/irs-drop, publications at
    irs.gov/pub/irs-pdf, Code sections and CFR sections at Cornell's Legal Information Institute. None when the
    address is not predictable (a public law, older guidance)."""
    if match := re.fullmatch(r"IRC (\d+[A-Z]?)", name):
        return f"https://www.law.cornell.edu/uscode/text/26/{match[1]}"
    if match := re.fullmatch(r"(Notice|Rev\. Proc\.|Rev\. Rul\.) (\d{4})-(\d+)", name):
        if int(match[2]) < 2020:
            return None
        prefix = {"Notice": "n", "Rev. Proc.": "rp", "Rev. Rul.": "rr"}[match[1]]
        return f"https://www.irs.gov/pub/irs-drop/{prefix}-{match[2][2:]}-{int(match[3]):02d}.pdf"
    if match := re.fullmatch(r"Pub\. (\d+)", name):
        return f"https://www.irs.gov/pub/irs-pdf/p{match[1]}.pdf"
    if match := re.fullmatch(r"(\d+) CFR (\d+\.\d+)", name):
        return f"https://www.law.cornell.edu/cfr/text/{match[1]}/{match[2]}"
    if name == "Schedule 1-A":
        return "https://www.irs.gov/pub/irs-pdf/f1040s1a.pdf"
    if match := re.fullmatch(r"P\.L\. (\d+)-(\d+)", name):
        return f"https://www.govinfo.gov/link/plaw/{match[1]}/public/{match[2]}"  # GovInfo answers 400 for a law that does not exist
    return None


def _status(url: str, timeout: float) -> int | None:
    """The HTTP status a HEAD request to the address returns, or None when no answer came."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "taxdiff (synthetic case study)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return None


def check_citation(cite: str, timeout: float = ECFR_TIMEOUT) -> dict[str, object]:
    """Look up every authority a citation names that has a predictable address. "not_found" when one answers
    404 or 410 (the change cannot be approved on it), "found" when each one looked up is there, "not_checked"
    when none could be looked up or no answer came."""
    looked: list[tuple[str, str, int | None]] = []
    for name in named_authorities(cite)[:3]:
        url = authority_url(name)
        if url is not None:
            looked.append((name, url, _status(url, timeout)))
    for name, url, status in looked:
        if status in (404, 410) or (status == 400 and "govinfo.gov/link/plaw" in url):
            return {"state": "not_found", "detail": f"{name} was not found at {url} (HTTP {status})", "url": url}
    found = [(name, url) for name, url, status in looked if status is not None and 200 <= status < 400]
    if looked and len(found) == len(looked):
        return {"state": "found", "detail": ", ".join(name for name, _ in found), "url": found[0][1]}
    if looked:
        missing = next(name for name, _, status in looked if status is None or not 200 <= status < 400)
        return {"state": "not_checked", "detail": f"no clear answer from the address of {missing}", "url": None}
    return {"state": "not_checked", "detail": "no authority in the citation has an address that can be looked up", "url": None}


def _checked(items: list[Authority], timeout: float = ECFR_TIMEOUT) -> list[Authority]:
    """Each cited source with what a live request to its address found; eCFR entries are already live."""

    def check(item: Authority) -> Authority:
        if item.fetched or item.key in FLSA or not item.url.startswith("https://"):
            return item
        try:
            return replace(item, checked=_head(item.url, timeout))
        except (OSError, ValueError) as exc:
            return replace(item, checked=f"no answer from its address ({type(exc).__name__})")

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(check, items))


_guidance: dict[str, tuple[float, list[Authority]]] = {}  # query -> (when it was fetched, documents), kept an hour


def guidance(paths: Iterable[str], timeout: float = ECFR_TIMEOUT) -> list[Authority]:
    """Federal Register documents from Treasury and the IRS on the provisions these rule paths belong to,
    newest first, three per provision. A search that finds nothing returns one entry that says so."""
    found: list[Authority] = []
    for prefix in dict.fromkeys(prefix for path in paths for prefix in GUIDANCE if path.startswith(prefix)):
        phrase, topic = GUIDANCE[prefix]
        cached = _guidance.get(phrase)
        if cached is not None and perf_counter() - cached[0] < 3600:
            found += cached[1]
            continue
        query = urllib.parse.urlencode([("per_page", "3"), ("order", "newest"), ("conditions[term]", f'"{phrase}"'),
                                        ("conditions[agencies][]", "internal-revenue-service"), ("conditions[agencies][]", "treasury-department"),
                                        *[("fields[]", name) for name in ("title", "type", "publication_date", "html_url", "document_number")]])
        url = f"{FEDERAL_REGISTER}?{query}"
        try:
            body, line = _get(url, timeout)
            data = json.loads(body)
            docs = [Authority(f"fr-{d['document_number']}", f"Federal Register, {d['publication_date']}: {d['type']}, {d['title']}",
                              str(d["html_url"]), f"{topic}: Treasury and IRS guidance, searched live", fetched=True, request=line)
                    for d in data.get("results", [])[:3]]
            if not docs:
                docs = [Authority(f"fr-none-{prefix.rstrip('.')}", f"Federal Register: no Treasury or IRS document mentions \"{phrase}\"",
                                  "https://www.federalregister.gov/", f"{topic}: searched live", fetched=True, request=line)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            docs = [Authority(f"fr-failed-{prefix.rstrip('.')}", f"Federal Register search for \"{phrase}\" did not answer",
                              "https://www.federalregister.gov/", f"{topic}: not searched", fallback=f"{type(exc).__name__}")]
            found += docs
            continue
        _guidance[phrase] = (perf_counter(), docs)
        found += docs
    return found


def for_rules(paths: Iterable[str], live: bool = False) -> list[Authority]:
    paths = list(paths)
    keys = list(dict.fromkeys(key for path in paths for prefix, found in BY_PREFIX.items() if path.startswith(prefix) for key in found))
    table = _with_live(keys) if live else authorities()
    items = [table[key] for key in keys]
    if not live:
        return items
    with ThreadPoolExecutor(max_workers=2) as pool:
        checked, related = pool.submit(_checked, items), pool.submit(guidance, paths)
        return checked.result() + related.result()


def for_lines(lines: Iterable[str], live: bool = False) -> list[Authority]:
    keys = list(dict.fromkeys(key for line in lines for key in BY_LINE.get(line, ())))
    table = _with_live(keys) if live else authorities()
    return [table[key] for key in keys]
