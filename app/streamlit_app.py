"""TaxDiff review screen. Run: streamlit run app/streamlit_app.py"""

from __future__ import annotations

import html
import importlib.util
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from taxdiff.changes import ChangeSet, load_change  # noqa: E402
from taxdiff.engine import LINE_NUMBERS, LINES  # noqa: E402
from taxdiff.facts import ClientReturn, load_book, parse_path  # noqa: E402
from taxdiff.lanes import intake, law, llm  # noqa: E402
from taxdiff.lanes.engine_b import load_cache  # noqa: E402
from taxdiff.lanes.notes import money  # noqa: E402
from taxdiff.paths import BOOK, CHANGES, RULES  # noqa: E402
from taxdiff.provenance import Unknown  # noqa: E402
from taxdiff.provisions import OVERTIME  # noqa: E402
from taxdiff.router import ROUTES  # noqa: E402
from taxdiff.rules import load_pack  # noqa: E402
from taxdiff.workflow import LaneResult, Run, State, approve_run, initial_state, record_intake_refusal, run_change  # noqa: E402

st.set_page_config(page_title="TaxDiff", layout="wide")
st.markdown(f"<style>{(Path(__file__).parent / 'style.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

# The table styler cannot read CSS custom properties, so route and parity colors are repeated here as hex.
ROUTE_HEX = {"update": "#0b5cad", "reviewer": "#9a6700", "amendment": "#b42318", "document": "#6f42c1", "below_materiality": "#676767", "no_change": "#676767"}
PARITY = {"agree": ("Agrees", "#1a7f37"), "differs": ("Differs", "#b42318"), "not_checked": ("Not checked", "#676767")}
LANE_VAR = {"done": "--lane-done", "failed": "--lane-failed", "waiting": "--lane-waiting", "skipped": "--lane-skipped"}
LANE_NAMES = {
    "intake": "Intake",
    "documents": "Documents",
    "validate": "Validate",
    "approve": "Approve",
    "engine_a": "Engine A",
    "engine_b": "Engine B, PolicyEngine-US",
    "law": "Law",
    "route": "Route",
    "notes": "Notes",
}
STAGES = [["intake"], ["documents"], ["validate"], ["engine_a"], ["engine_b", "law"], ["route"], ["approve"], ["notes"]]
LINE_NO = LINE_NUMBERS


def part(label: str, title: str = "") -> None:
    st.markdown(f'<div class="part"><span class="label">{label}</span><span class="title">{html.escape(title)}</span></div>', unsafe_allow_html=True)


def board_html(by_name: dict[str, LaneResult], animate: bool) -> str:
    """The lane board. Lanes not reported yet show as waiting; animate replays the order the lanes ran."""
    cells = ['<div class="board">']
    for n, stage in enumerate(STAGES):
        column = []
        for name in stage:
            lane = by_name.get(name)
            if lane is None:
                column.append(f'<div class="lane idle"><div class="name">{LANE_NAMES[name]}</div><div class="meta">waiting</div></div>')
                continue
            bar = '<div class="bar"></div>' if animate else '<div class="bar" style="animation: none"></div>'
            column.append(
                f'<div class="lane" style="--state: var({LANE_VAR[lane.state]}); --i: {n}">{bar}'
                f'<div class="name">{LANE_NAMES[name]}</div>'
                f'<div class="meta"><span class="dot"></span>{lane.state} · {lane.ms:.1f} ms</div>'
                f'<div class="sum">{html.escape(lane.summary)}</div></div>'
            )
        cells.append(f'<div class="stage">{"".join(column)}</div>')
        if n < len(STAGES) - 1:
            cells.append('<div class="arrow">→</div>')
    cells.append("</div>")
    return "".join(cells)


@st.cache_resource
def base():
    return load_pack(RULES), load_book(BOOK)


@st.cache_data(ttl=30)
def providers_available() -> dict[str, bool]:
    return llm.available()


def reset(parity_mode: str) -> None:
    """Back to rule pack v1 and the original facts. The audit log is kept."""
    pack, book = base()
    kept = st.session_state.state.audit if "state" in st.session_state else ()
    st.session_state.state = replace(initial_state(pack, book, parity_mode), audit=kept)
    st.session_state.runs = []
    st.session_state.written = 100
    st.session_state.proposal = None
    st.session_state.previewed = None


if "state" not in st.session_state:
    reset("cache")

state: State = st.session_state.state
runs: list[Run] = st.session_state.runs
last: Run | None = runs[-1] if runs else None  # the latest run of any kind: drives the lane board
applied = [run for run in runs if run.applied]
done: Run | None = applied[-1] if applied else None  # the last applied run: drives the book's routes
# The impact list and return detail show the latest preview or applied run; a preview shows its trial state.
shown: Run | None = last if last is not None and (last.applied or last.pending is not None) else done
view: State = shown.pending if shown is not None and shown.pending is not None else state
routes = {item.client_id: item.route for item in done.report.reached} if done and done.report else {}
available = providers_available()

# Header -------------------------------------------------------------------------------------------
counts = {key: sum(p.state == key for p in state.parity.values()) for key in PARITY}
cache = load_cache()
cached_at = cache["runs"][-1]["at"].replace("T", " ") if cache["runs"] else "never"
st.title("TaxDiff")
st.markdown(
    '<div class="sub">Which prepared returns a rule or fact change reaches, which lines move, and what happens to each return next.</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="facts">'
    f"<span><b>SYNTHETIC</b> book, tax year {state.book.tax_year}, {len(state.results)} returns</span>"
    f"<span>Rule pack <b>v{state.pack.version}</b>; qualified overtime: {html.escape(OVERTIME[state.pack.value('overtime.definition')].summary)}</span>"
    f"<span>PolicyEngine-US: <b>{counts['agree']}</b> agree, <b>{counts['differs']}</b> differ, <b>{counts['not_checked']}</b> not checked (cached {cached_at})</span>"
    + (
        f"<span>Filed returns: all <b>{len(state.as_filed)}</b> recompute to their filed amounts</span>"
        if not state.drift
        else f"<span>Filed returns: <b>{len(state.drift)}</b> no longer recompute to their filed amounts ({', '.join(sorted(state.drift))})</span>"
    )
    + "<span>Not modeled: AMT, capital-gain rates, credits, QBI, SALT, car-loan interest, state returns, e-file; for the standard "
    "deduction, blindness, a dependent's limited amount and the zero amount when a separately filing spouse itemizes</span>"
    "</div>",
    unsafe_allow_html=True,
)

# Book ---------------------------------------------------------------------------------------------
part("Book", f"{len(state.results)} returns: status, money lines, parity with PolicyEngine-US, route after the last change")
rows = []
for client_id, result in sorted(state.results.items()):
    values = result.values

    def num(line: str, values=values):
        value = values[line]
        return None if isinstance(value, Unknown) else float(value)

    rows.append(
        {
            "Client": client_id,
            "Status": result.status,
            "Filing": result.filing_status.upper(),
            "AGI": num("agi"),
            "Schedule 1-A": num("sch1a_total"),
            "Taxable income": num("taxable_income"),
            "Tax, line 16": num("tax"),
            "Facts": f"v{result.facts_version}",
            "Parity": PARITY[state.parity[client_id].state][0],
            "Route": ROUTES.get(routes.get(client_id, ""), ""),
        }
    )


def color_route(value: str) -> str:
    key = next((k for k, label in ROUTES.items() if label == value), None)
    return f"color: {ROUTE_HEX[key]}; font-weight: 700" if key else ""


def color_parity(value: str) -> str:
    key = next((k for k, (label, _) in PARITY.items() if label == value), None)
    return f"color: {PARITY[key][1]}" if key else ""


money_cols = {name: st.column_config.NumberColumn(name, format="dollar") for name in ("AGI", "Schedule 1-A", "Taxable income", "Tax, line 16")}
st.dataframe(
    pd.DataFrame(rows).style.map(color_route, subset=["Route"]).map(color_parity, subset=["Parity"]),
    hide_index=True,
    height=330,
    width="stretch",
    column_config=money_cols,
)
st.caption("A blank money cell is Unknown: a fact the line needs is missing, and the return waits for a document.")

# Change -------------------------------------------------------------------------------------------
part("Change", "an example change set or a written instruction; validated and previewed with amounts and routes, applied only on approval")
left, right = st.columns([3, 2])
with left:
    source = st.radio("Source", ["Example change set", "Write a change"], horizontal=True, label_visibility="collapsed")
    change: ChangeSet | None = None
    intake_lane: LaneResult | None = None
    if source == "Example change set":
        files = sorted(CHANGES.glob("*.yaml"))
        pick = st.selectbox("Change set", files, format_func=lambda p: p.stem)
        change = load_change(pick)
        st.code(pick.read_text(encoding="utf-8"), language="yaml")
    else:
        choices = ["pattern"] + [name for name in ("anthropic", "ollama") if available[name]]
        provider = st.radio(
            "Read the text with",
            choices,
            horizontal=True,
            format_func=lambda name: {"pattern": "Pattern parser"}.get(name, llm.PROVIDERS.get(name, name)),
            help="LLM options appear when a key or a local Ollama server is available. Every number and citation they propose must be in the text; "
            "the proposal then goes through the same validation, preview and approval.",
        )
        text = st.text_area(
            "Instruction",
            placeholder="set overtime.cap.other to 2500 per Hypothetical change for testing\nset C-019 people[0].ot_flsa_hours to 150",
            height=90,
        )
        # The proposal is made once, on request, and stored: approval applies exactly the proposal shown.
        if st.button("Read instruction", disabled=not text.strip()):
            change_id = f"CS-{st.session_state.written:03d}"
            started = perf_counter()
            try:
                with st.spinner("Reading the instruction"):
                    proposal = intake.propose(text, change_id, provider)
                edits = len(proposal.rule_edits) + len(proposal.fact_edits)
                label = "pattern parser" if provider == "pattern" else f"{llm.PROVIDERS[provider]}, proposal pending approval"
                lane = LaneResult("intake", "done", (perf_counter() - started) * 1000, f"{label}: {edits} edit(s)")
                st.session_state.proposal = {"text": text, "provider": provider, "change": proposal, "lane": lane}
            except (ValueError, llm.Unavailable) as exc:
                st.session_state.proposal = None
                st.session_state.state = record_intake_refusal(state, change_id, text, provider, str(exc))
                st.session_state.previewed = None  # the state moved on, so an earlier preview can no longer be approved
                st.error(f"{exc}. The refusal is in the audit log.")
        stored = st.session_state.get("proposal")
        if stored and stored["text"] == text and stored["provider"] == provider:
            change, intake_lane = stored["change"], stored["lane"]
            st.code(change.model_dump_json(indent=1, exclude_defaults=True), language="json")
        elif text.strip():
            st.caption("Read the instruction to see the proposed change set before it can be previewed or approved.")
with right:
    approver = st.text_input("Approver", placeholder="Your name", help="Recorded in the audit log with every approved change.")
    materiality = st.number_input("Materiality for filed returns ($)", min_value=0, value=50, step=10)
    live_ok = importlib.util.find_spec("policyengine_us") is not None
    parity_choice = st.radio(
        "PolicyEngine-US parity",
        ["Cached results", "Run live (local, about 2 minutes)", "Off"] if live_ok else ["Cached results", "Off"],
        help="Returns PolicyEngine-US has not checked route by status alone."
        + ("" if live_ok else " Live runs need PolicyEngine-US installed locally."),
    )
    parity_mode = {"Cached results": "cache", "Off": "off"}.get(parity_choice, "live")
    docling_ok = importlib.util.find_spec("docling") is not None
    docling_live = st.checkbox(
        "Convert PDF statements again with Docling",
        disabled=not docling_ok,
        help="By default a PDF converted once is read from the cached conversion. Docling's first conversion took 52 s here."
        if docling_ok
        else "Docling is not installed here; PDFs are read from cached conversions.",
    )
    note_choices = [None] + [name for name in ("anthropic", "ollama") if available[name]]
    notes_provider = st.selectbox(
        "Reviewer note wording",
        note_choices,
        format_func=lambda name: "Templates" if name is None else f"Reworded by {llm.PROVIDERS[name]}",
        help="Reworded notes are kept only if their figures match the template in the same order and the route phrase survives. "
        "Client document requests are never reworded.",
    )
    # Approval applies the latest preview as it stands, so it is offered only while that preview matches the
    # change and settings on the screen.
    settings = (change.model_dump_json(), int(materiality), parity_mode, docling_live) if change is not None else None
    ready = last is not None and last.pending is not None and st.session_state.get("previewed") == settings
    b1, b2, b3 = st.columns(3)
    preview = b1.button("Preview", disabled=change is None, width="stretch")
    apply = b2.button(
        "Approve and apply",
        type="primary",
        disabled=not ready or not approver.strip(),
        width="stretch",
        help="Applies the routes and amounts of the latest preview. Preview the change with these settings and enter your name first.",
    )
    if b3.button("Reset book", width="stretch"):
        reset(parity_mode)
        st.rerun()
    if change is not None and not ready:
        st.caption("Preview this change with these settings to approve it.")

if (preview or apply) and change is not None:
    part("Lanes", f"{change.id}: running")
    live = st.empty()
    seen: dict[str, LaneResult] = {lane.lane: lane for lane in last.lanes} if apply and last is not None else {}

    def show(lane: LaneResult) -> None:
        seen[lane.lane] = lane
        live.markdown(board_html(seen, animate=False), unsafe_allow_html=True)

    with st.spinner("Running the lanes"):
        if apply and last is not None:
            run = approve_run(last, approver, notes_provider=notes_provider, on_lane=show)
        else:
            run = run_change(
                state,
                change,
                materiality=Decimal(int(materiality)),
                parity_mode=parity_mode,  # type: ignore[arg-type]
                intake=intake_lane,
                docling_live=docling_live,
                on_lane=show,
            )
            st.session_state.previewed = settings
    st.session_state.runs.append(run)
    if run.state is not None:
        st.session_state.state = run.state  # applied changes, and the audit entry for rejected or previewed ones
    if run.applied and source == "Write a change":
        st.session_state.written += 1
        st.session_state.proposal = None
    st.rerun()

# Lane board ---------------------------------------------------------------------------------------
if last is not None:
    part("Lanes", f"{last.change.id}: {last.change.title}")
    st.markdown(board_html({lane.lane: lane for lane in last.lanes}, animate=True), unsafe_allow_html=True)
    st.caption(
        "Lanes run in the order their inputs allow: Engine A first, then Engine B and Law side by side; routing waits for "
        "Engine B, and nothing is applied before the approve lane. Durations are measured on this run, and the bars replay "
        "the order the lanes ran. Engine B reads cached PolicyEngine-US results unless run live."
    )
    if last.validation and last.validation.errors:
        st.error("Not applied. The last valid version stays in effect.\n\n" + "\n".join(f"- {e}" for e in last.validation.errors))
    elif not last.applied:
        st.info("Preview: the routes and amounts below are what approval would apply. Nothing has changed yet.")

# Impact list --------------------------------------------------------------------------------------
if shown is not None and shown.report is not None:
    report = shown.report
    status = "applied" if shown.applied else "preview, not applied"
    part("Impact", f"{shown.change.id} ({status}): {len(report.reached)} of {len(report.results)} returns reached; {len(report.not_reached)} not reached by recorded reads; full recompute found {len(report.unexpected)} unexpected changes")
    impact = pd.DataFrame(
        [
            {
                "Client": item.client_id,
                "Status": item.status,
                "Route": ROUTES[item.route],
                "Lines changed": ", ".join(LINES[d.line].split(",")[0] for d in item.diffs if d.line not in ("magi", "tax_formula")),
                "Tax change": None if item.delta_tax is None else float(item.delta_tax),
                "Against filed": None if item.filed_delta_tax is None else float(item.filed_delta_tax),
                "Parity": PARITY[view.parity[item.client_id].state][0],
                "Needs": "; ".join(item.needs),
            }
            for item in report.reached
        ]
    )
    if not impact.empty:
        st.dataframe(
            impact.style.map(color_route, subset=["Route"]).map(color_parity, subset=["Parity"]),
            hide_index=True,
            width="stretch",
            column_config={"Tax change": st.column_config.NumberColumn(format="dollar"), "Against filed": st.column_config.NumberColumn(format="dollar")},
        )
    if shown.authorities:  # the Law lane's output for this change
        st.markdown("Authorities for this change: " + " · ".join(f"[{a.title}]({a.url})" for a in shown.authorities))

# Return detail ------------------------------------------------------------------------------------
reached_ids = [item.client_id for item in shown.report.reached] if shown and shown.report else []
choices = reached_ids + [cid for cid in sorted(view.results) if cid not in reached_ids]
part("Return", "line values before and after the change shown above, what each line read, and the note")
client_id = st.selectbox("Client", choices, index=0)
result = view.results[client_id]
ret: ClientReturn = view.book.get(client_id)  # type: ignore[assignment]
item = next((i for i in (shown.report.reached if shown and shown.report else []) if i.client_id == client_id), None)
parity = view.parity[client_id]
filed = view.as_filed.get(client_id)
st.markdown(
    f"{client_id} · {result.status} · {result.filing_status.upper()} · rule pack v{result.pack_version} · facts v{result.facts_version} · "
    f"PolicyEngine-US: **{PARITY[parity.state][0]}**" + (f" ({parity.reason})" if parity.reason and parity.reason != "lines differ" else "")
)
filed_head = '<span class="amt">As filed</span>' if filed else ""
lines_html = [f'<div class="lines{" filed" if filed else ""}"><div class="ln head"><span class="label">Line</span><span class="no">No.</span>{filed_head}<span class="amt">Before</span><span class="amt">After</span></div>']
for line, label in LINES.items():
    after = result.values[line]
    before = item.before.values[line] if item else after
    changed = " changed" if before != after else ""
    filed_cell = f'<span class="amt filed">{money(filed[line]) if line in filed else ""}</span>' if filed else ""
    lines_html.append(
        f'<div class="ln{changed}"><span class="label"><span>{html.escape(label.split(",")[0])}</span><i></i></span>'
        f'<span class="no">{LINE_NO[line]}</span>{filed_cell}<span class="amt">{money(before)}</span><span class="amt after">{money(after)}</span></div>'
    )
lines_html.append("</div>")
st.markdown("".join(lines_html), unsafe_allow_html=True)

if parity.state == "differs":
    st.markdown("Engine A and PolicyEngine-US disagree on:")
    st.dataframe(
        pd.DataFrame([{"Line": LINES[line], "Engine A": money(ours), "PolicyEngine-US": money(theirs)} for line, ours, theirs in parity.diffs]),
        hide_index=True,
    )

if shown and shown.applied and client_id in shown.notes:
    st.markdown(f'<div class="note">{html.escape(shown.notes[client_id])}</div><div class="source">Note text: {html.escape(shown.note_sources.get(client_id, "template"))}</div>', unsafe_allow_html=True)

line = st.selectbox("What a line read", list(LINES), format_func=lambda key: LINES[key], index=list(LINES).index("sch1a_overtime"))
facts = ret.model_dump(mode="json")


def fact_value(path: str):
    target = facts
    for part_ in parse_path(path):
        target = target[part_]
    return target


read_rows = []
for path in sorted(result.reads[line]):
    kind, _, key = path.partition(":")
    if kind == "rule":
        value = view.pack.value(key)
        read_rows.append({"Read": "rule", "Name": key, "Value": "rate schedule" if isinstance(value, tuple) else str(value), "Source": view.pack.cite(key)})
    elif kind == "fact":
        value = fact_value(key) if key != "people" else len(ret.people)
        read_rows.append({"Read": "fact", "Name": key, "Value": "empty" if value is None else str(value), "Source": f"client facts v{ret.facts_version}"})
    else:
        read_rows.append({"Read": "definition", "Name": key, "Value": "", "Source": "provisions.py"})
st.dataframe(pd.DataFrame(read_rows), hide_index=True, width="stretch")

for authority in law.for_lines([line]):
    if authority.excerpt:
        st.markdown(
            f'<div class="excerpt"><b><a href="{authority.url}">{html.escape(authority.title)}</a></b> (eCFR, as of {authority.as_of})<br>'
            f"{html.escape(authority.excerpt[:420])}{'...' if len(authority.excerpt) > 420 else ''}</div>",
            unsafe_allow_html=True,
        )
plain = [a for a in law.for_lines([line]) if not a.excerpt]
if plain:
    st.markdown("Authorities: " + " · ".join(f"[{a.title}]({a.url})" for a in plain))

# Audit log ----------------------------------------------------------------------------------------
audit = st.session_state.state.audit  # read again: an intake refusal earlier in this run adds an entry
with st.expander(f"Audit log ({len(audit)} {'entry' if len(audit) == 1 else 'entries'})"):
    if audit:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "At": e.at,
                        "Change": e.change_id,
                        "Title": e.title,
                        "Outcome": e.outcome,
                        "Approver": e.approver,
                        "Pack": f"v{e.pack_version} ({e.pack_sha})",
                        "Facts": e.facts,
                        "Materiality": e.materiality,
                        "Parity": e.parity,
                        "Engine": e.engine,
                        "Detail": e.detail,
                    }
                    for e in audit
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.write("No changes yet.")
