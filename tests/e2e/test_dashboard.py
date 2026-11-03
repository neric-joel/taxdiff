"""Browser tests of the dashboard, against a running copy of it.

Skipped unless TAXDIFF_E2E_URL names the dashboard (http://localhost:3000 for `next dev` with the API on
port 8000, or the production URL). TAXDIFF_E2E_SHOTS names a folder for screenshots; without it none
are saved. Each test opens a fresh browser context, so its session starts at rules version 1, and every test
fails on a console error or a failed request.

  TAXDIFF_E2E_URL=http://localhost:3000 pytest tests/e2e -m e2e
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

URL = os.environ.get("TAXDIFF_E2E_URL", "").rstrip("/")
SHOTS = os.environ.get("TAXDIFF_E2E_SHOTS", "")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not URL, reason="set TAXDIFF_E2E_URL to run the browser tests")]

if URL:
    from playwright.sync_api import Browser, Page, expect, sync_playwright


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


class Watch:
    """Console errors and failed requests seen by one page."""

    def __init__(self, page: Page) -> None:
        self.errors: list[str] = []
        page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("requestfailed", lambda request: self.errors.append(f"request failed: {request.method} {request.url} {request.failure}"))
        page.on("response", lambda response: self.errors.append(f"{response.status} {response.url}") if response.status >= 400 else None)


def open_page(browser: Browser, *, width: int = 1440, height: int = 900, reduced: bool = False, path: str = "") -> tuple[Page, Watch]:
    context = browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce" if reduced else "no-preference")
    page = context.new_page()
    watch = Watch(page)
    page.goto(URL + path, wait_until="networkidle")
    start = page.get_by_role("button", name="Start")
    if start.is_visible():
        start.click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    return page, watch


def shot(page: Page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


def chip(page: Page, label: str):
    """An example in the question panel, by its plain words. Its name may add the change set id it runs
    ("(CS-001)") and, while the session lacks a change it builds on, ", needs CS-001 first"."""
    return page.locator(".ask .chips").get_by_role("button", name=re.compile(rf"^{re.escape(label)}( \(CS-\d+\))?(, needs CS-\d+ first)?$"))


def settled(page: Page) -> None:
    expect(page.locator("[data-run-status]")).not_to_have_attribute("data-run-status", re.compile(r"^(none|running)$"), timeout=90_000)


def run_example(page: Page, label: str) -> None:
    chip(page, label).click()
    settled(page)


def shown(page: Page, status: str) -> None:
    expect(page.locator("[data-shown-status]")).to_have_attribute("data-shown-status", status, timeout=30_000)


def show_steps(page: Page) -> None:
    """After a run the steps fold into one line; open them. The choice is remembered, so later runs stay open."""
    steps = page.locator(".graph-panel")
    toggle = steps.get_by_role("button", name="Show steps")
    if toggle.count():
        toggle.click()
    expect(steps).to_have_attribute("data-open", "true")
    expect(steps.locator(".lane").first).to_be_visible()


def approve(page: Page, name: str = "E2E Reviewer") -> None:
    gate = page.locator("#approval")
    expect(gate).to_be_visible()
    button = gate.get_by_role("button", name="Approve and apply")
    expect(button).to_be_disabled()
    expect(gate).to_contain_text("Type your name to approve or turn down.")
    gate.get_by_label("Your name, recorded in the audit log").fill(name)
    expect(button).to_be_enabled()
    button.click()
    expect(page.locator("[data-run-status]")).to_have_attribute("data-run-status", "applied", timeout=90_000)
    shown(page, "applied")
    # The approval is replaced in place by one line, and focus moves to it.
    expect(gate).to_have_count(0)
    line = page.locator("#decision")
    expect(line).to_contain_text(f"applied by {name}")
    expect(line).to_be_focused()


def apply_cs_001(page: Page) -> None:
    run_example(page, CS_001)
    shown(page, "waiting")
    approve(page)


def node(page: Page, name: str):
    return page.locator(f'.graph-panel .lane[aria-label^="{name}:"]')


def panel(page: Page, heading_id: str):
    return page.locator(f'section[aria-labelledby="{heading_id}"]')


def result_tab(page: Page, name: str):
    """A tab under the result: Impact list, Notes and letters or Audit log, named with its count when it has one."""
    return page.get_by_role("tab", name=re.compile(rf"^{re.escape(name)}( \d+)?$"))


def open_tab(page: Page, name: str) -> None:
    """The impact list, the notes and letters and the audit log are tabs under the result; open one."""
    tab = result_tab(page, name)
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true")


WHAT_IF = "What if the overtime cap drops to $10,000?"
CS_001 = "Apply the IRS overtime rule"
# Examples about the returns after CS-001: in a fresh session each offers to run CS-001 first.
NEEDS_CS_001 = {
    "Read C-017's corrected employer statement",
    "Did C-017's tax go down?",
    "Why did C-031's tax go down?",
    "Which filed returns need amending?",
}

EXPECTED = {
    "Where C-017's overtime deduction comes from": "answered",
    CS_001: "waiting",
    "Which filed returns need amending?": "answered",
    "Read C-017's corrected employer statement": "waiting",
    "Apply C-021's corrected W-2 (wages $149,000)": "waiting",
    "Did C-017's tax go down?": "answered",
    "Why did C-031's tax go down?": "answered",
    "Why C-010's overtime deduction is zero": "answered",
    WHAT_IF: "what_if",
    "Refuse a negative cap with no citation": "rejected",
    "Refuse a statement for the wrong year": "rejected",
}


@pytest.mark.parametrize("label", list(EXPECTED))
def test_each_example_runs_to_the_end(browser, label):
    page, watch = open_page(browser)
    if label in NEEDS_CS_001:
        # The example says what it needs; a click offers that change instead of answering about the wrong returns.
        expect(chip(page, label)).to_contain_text("Run CS-001 first")
        chip(page, label).click()
        page.locator(".offer").get_by_role("button", name="Run CS-001 first").click()
        settled(page)
        shown(page, "waiting")
        approve(page)
        expect(chip(page, label)).not_to_contain_text("Run CS-001 first")
    run_example(page, label)
    shown(page, EXPECTED[label])
    if EXPECTED[label] == "waiting":
        approve(page)
    if EXPECTED[label] == "what_if":
        expect(page.locator("#approval")).to_have_count(0)
    expect(page.get_by_role("heading", name="Answer", exact=True)).to_be_visible()
    shot(page, f"example-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}")
    assert watch.errors == []
    page.context.close()


def test_the_first_screen_is_one_sentence_and_the_examples(browser):
    page, watch = open_page(browser)
    band = page.locator(".problem")
    expect(band).to_have_attribute("data-open", "true")
    expect(band.locator(".lede")).to_have_text(re.compile(r"^When a tax rule or a client's documents change after returns are prepared"))
    # The rest of the explainer is behind How it works.
    how = band.get_by_role("button", name="How it works")
    expect(how).to_have_attribute("aria-expanded", "false")
    expect(band.locator(".problem-steps")).to_be_hidden()
    how.click()
    expect(band.locator(".problem-steps")).to_be_visible()
    expect(band).to_contain_text("the extra half of the pay rate for overtime hours")
    how.click()
    # Plain words on the reviewer's screen.
    scope = page.locator(".scope")
    expect(scope).to_contain_text("Rules: 2025, version 1")
    expect(scope).not_to_contain_text("Facts:")
    expect(page.locator(".ask .firm-line")).to_contain_text("Amend threshold $50")
    # Four groups of examples at most, each chip the column's width, the change set id last.
    expect(page.locator(".ask .chip-group")).to_have_count(4)
    widths = page.evaluate("() => [...document.querySelectorAll('.ask .chip')].map((c) => Math.round(c.getBoundingClientRect().width))")
    assert len(set(widths)) == 1, widths
    expect(chip(page, CS_001).locator(".chip-code")).to_have_text("CS-001")
    # Under 300 words on the first screen at 1440 x 900, figures in the table included.
    words = page.evaluate(
        """() => {
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          const range = document.createRange();
          let count = 0;
          for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const text = n.textContent.trim();
            const el = n.parentElement;
            if (!text || !el || el.closest('.sr-only, [hidden], script, style')) continue;
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            range.selectNodeContents(n);
            const box = range.getBoundingClientRect();
            if (box.width === 0 || box.bottom < 0 || box.top > window.innerHeight) continue;
            count += text.split(/\\s+/).filter((w) => /[A-Za-z0-9$]/.test(w)).length;
          }
          return count;
        }"""
    )
    assert words < 300, words
    shot(page, "first-screen")
    assert watch.errors == []
    page.context.close()


def test_the_steps_fold_after_a_run_and_remember_the_toggle(browser):
    page, watch = open_page(browser)
    steps = page.locator(".graph-panel")
    # Before any run the explainer shows and the steps stay folded; they open while a run streams.
    expect(steps).to_have_attribute("data-open", "false")
    expect(page.locator(".problem")).to_have_attribute("data-open", "true")
    run_example(page, "Why C-010's overtime deduction is zero")
    shown(page, "answered")
    # Result first: the steps fold into one line with the outcome only, and the answer takes focus.
    expect(steps).to_have_attribute("data-open", "false")
    expect(steps.locator(".lane")).to_have_count(0)
    expect(steps.locator(".run-status")).to_contain_text("Answered about C-010")
    expect(steps.locator(".run-status")).not_to_contain_text("on the server")
    expect(page.locator("#answer-title")).to_be_focused()
    expect(page.locator(".problem")).to_have_attribute("data-open", "false")
    expect(page.locator(".problem").get_by_role("button", name="What this does")).to_have_attribute("aria-expanded", "false")
    toggle = steps.get_by_role("button", name="Show steps")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(steps).to_have_attribute("data-open", "true")
    # Step counts and the server's time belong to the open steps view.
    expect(steps.locator(".run-status")).to_contain_text(re.compile(r"12 steps: \d+ "))
    expect(steps.locator(".run-status")).to_contain_text("on the server")
    expect(node(page, "Answer")).to_have_attribute("data-state", "done")
    # The choice survives a reload, which restores the last run.
    page.reload(wait_until="networkidle")
    expect(page.locator(".graph-panel")).to_have_attribute("data-open", "true")
    expect(page.locator(".graph-panel").get_by_role("button", name="Hide steps")).to_have_attribute("aria-expanded", "true")
    assert watch.errors == []
    page.context.close()


def test_the_result_details_are_tabs(browser):
    page, watch = open_page(browser)
    impact, notes, audit = (result_tab(page, name) for name in ("Impact list", "Notes and letters", "Audit log"))
    # Fewer sections at once: the impact list is in view, the notes and the audit log wait behind their tabs. Each
    # panel keeps its section and heading.
    expect(impact).to_have_attribute("aria-selected", "true")
    expect(panel(page, "impact-title")).to_be_visible()
    expect(panel(page, "notes-title")).to_be_hidden()
    expect(panel(page, "audit-title")).to_be_hidden()
    # Arrow keys, Home and End move and select, as in the side panel.
    impact.focus()
    page.keyboard.press("ArrowRight")
    expect(notes).to_be_focused()
    expect(notes).to_have_attribute("aria-selected", "true")
    expect(panel(page, "notes-title")).to_be_visible()
    expect(page.get_by_role("heading", name="Notes and letters", exact=True)).to_have_count(1)
    page.keyboard.press("End")
    expect(audit).to_be_focused()
    expect(panel(page, "audit-title")).to_be_visible()
    expect(panel(page, "impact-title")).to_be_hidden()
    page.keyboard.press("ArrowRight")
    expect(impact).to_be_focused()
    page.keyboard.press("ArrowLeft")
    expect(audit).to_be_focused()
    page.keyboard.press("Home")
    expect(impact).to_have_attribute("aria-selected", "true")
    # A new run opens on the impact list, its tab counting the returns reached.
    open_tab(page, "Audit log")
    run_example(page, CS_001)
    shown(page, "waiting")
    expect(impact).to_have_attribute("aria-selected", "true")
    expect(impact).to_have_text("Impact list 15")
    assert watch.errors == []
    page.context.close()


def test_one_return_start_to_finish_comes_first(browser):
    page, watch = open_page(browser)
    run_example(page, CS_001)
    shown(page, "waiting")
    # The result opens with one return told from the change to its next step, then who the change reaches, then the
    # decision; the guide line above it marks the decision as the current step.
    story = page.locator("#story")
    expect(story.get_by_role("heading", name="One return, start to finish")).to_be_visible()
    expect(story).to_contain_text(re.compile(r"from \$[\d,]+ to \$[\d,]+"))
    expect(page.locator("#guide [aria-current='step']")).to_contain_text("Your decision")
    tops = page.evaluate("() => ['#story', '#kpi-title', '#approval'].map((s) => document.querySelector(s).getBoundingClientRect().top)")
    assert tops == sorted(tops), tops
    # Its client opens the return.
    client = story.get_by_role("button").first
    name = client.inner_text()
    client.click()
    expect(page.get_by_role("dialog", name=f"Return {name}")).to_be_visible()
    page.keyboard.press("Escape")
    approve(page)
    expect(page.locator("#guide li[data-state='done']")).to_have_count(3)
    expect(page.locator("#decision .decision-mark")).to_be_visible()
    expect(page.locator("#story")).to_contain_text("goes from")
    assert watch.errors == []
    page.context.close()


def test_history_is_a_tab_beside_the_question(browser):
    page, watch = open_page(browser)
    # The side column scrolls with the page: no scroll area of its own.
    assert page.evaluate("() => getComputedStyle(document.querySelector('.side')).overflowY") == "visible"
    run_example(page, "Why C-010's overtime deduction is zero")
    shown(page, "answered")
    question = page.get_by_role("tab", name="Question or change")
    expect(question).to_have_attribute("aria-selected", "true")
    question.focus()
    page.keyboard.press("ArrowRight")
    history = page.get_by_role("tab", name="History (1)")
    expect(history).to_be_focused()
    expect(history).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pane-history")).to_be_visible()
    expect(page.locator("#pane-history .history")).to_contain_text("Answered about C-010")
    expect(page.locator("#pane-ask")).to_be_hidden()
    page.keyboard.press("Home")
    expect(question).to_be_focused()
    expect(page.locator("#pane-ask")).to_be_visible()
    assert watch.errors == []
    page.context.close()


def test_the_approve_gate_pauses_the_run_and_resumes_it(browser):
    page, watch = open_page(browser)
    run_example(page, CS_001)
    shown(page, "waiting")
    # The approval is the decision surface: across the main column, focused, saying what approving does.
    expect(page.locator("#gate-title")).to_be_focused()
    gate = page.locator("#approval")
    expect(gate).to_contain_text("Technical approval of the rule change.")
    expect(gate).to_contain_text("Preview. Nothing changes until you approve.")
    expect(gate).to_contain_text("What you approve")
    expect(gate).to_contain_text("version 1 to version 2")
    expect(gate).to_contain_text("Amend threshold")
    expect(gate).to_contain_text("$50 (firm setting)")
    expect(gate).to_contain_text("What approving does")
    expect(gate).to_contain_text("Amendment review")
    expect(gate).to_contain_text("Letters asking for documents")
    expect(gate).to_contain_text("C-019")
    expect(gate).to_contain_text(re.compile(r"Writes 15 internal notes, \d+ client letters and an audit entry\."))
    # The tax rows live in the approval card while the preview waits, each with its count, and nowhere else.
    tax = gate.locator(".tax-rows")
    for label in ("More tax", "Less tax", "Waiting for a document", "Filed returns owing more", "Filed returns due a refund"):
        expect(tax).to_contain_text(label)
    expect(gate).to_contain_text("+ means more tax owed")
    # Both columns start on the same line.
    tops = page.evaluate("() => [...document.querySelectorAll('#approval .gate-col .gate-facts')].map((e) => Math.round(e.getBoundingClientRect().top))")
    assert len(set(tops)) == 1, tops
    expect(page.locator(".graph-panel").get_by_role("link", name="Go to the approval")).to_have_attribute("href", "#approval")
    # While the preview waits, the empty Answer is not shown and the notes wait behind their tab: the impact list is
    # the tab in view.
    expect(page.get_by_role("heading", name="Answer", exact=True)).to_have_count(0)
    expect(result_tab(page, "Impact list")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("heading", name="Notes and letters", exact=True)).to_have_count(0)
    # The reach strip: reached count, routes, book map and one cross-check line, about 120 px, no tax rows.
    reach = panel(page, "kpi-title")
    expect(reach.locator(".reach-count")).to_contain_text("15")
    expect(reach.locator(".routes-legend")).to_contain_text("Amendment review")
    expect(reach.locator(".parity-line")).to_contain_text("agree")
    expect(reach.locator(".parity-line")).not_to_contain_text("api.policyengine.org")
    expect(reach).not_to_contain_text("More tax")
    height = reach.evaluate("(e) => e.getBoundingClientRect().height")
    assert height <= 160, height
    unreached = reach.get_by_role("button", name="Returns not touched (25)")
    expect(unreached).to_have_attribute("aria-expanded", "false")
    unreached.click()
    expect(unreached).to_have_attribute("aria-expanded", "true")
    expect(reach.locator("#unreached-body .list li")).to_have_count(6)
    # The book map is a picture for the mouse; the returns are buttons in the tables.
    expect(page.locator(".bookmap")).to_have_attribute("aria-hidden", "true")
    expect(page.locator(".bookmap button")).to_have_count(0)
    expect(page.locator(".bookmap")).to_contain_text("40 sample returns")
    # The impact list is a worklist: grouped by route, most urgent first, the quiet returns folded.
    impact = panel(page, "impact-title")
    expect(impact.locator("tr.group").first).to_contain_text("Amendment review")
    expect(impact).to_contain_text("1 reached return: nothing changed")
    expect(impact).to_contain_text("Authority for every line below")
    expect(impact).to_contain_text("vs. the filed return")
    expect(impact).to_contain_text("Married filing jointly")
    # Nothing is signed off before the change is applied.
    expect(impact.get_by_label("Signing as")).to_have_count(0)
    shot(page, "gate-waiting")
    show_steps(page)
    expect(node(page, "Reviewer approval")).to_have_attribute("data-state", "waiting")
    expect(node(page, "Write notes")).to_have_attribute("data-state", "idle")
    approve(page)
    expect(page.locator("#decision")).to_contain_text("technical approval of the rule change")
    expect(page.locator("#decision")).to_contain_text("rules version 1 to 2")
    expect(node(page, "Reviewer approval")).to_have_attribute("data-state", "done")
    expect(node(page, "Write notes")).to_have_attribute("data-state", "done")
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 2")
    # After the decision the tax rows are in the answer.
    expect(page.locator(".answer-card")).to_contain_text("More tax")
    with page.expect_download() as download:
        impact.get_by_role("button", name="Export CSV").click()
    assert download.value.suggested_filename.endswith(".csv")
    open_tab(page, "Audit log")
    audit = page.locator("table.audit")
    expect(audit).to_contain_text("applied")
    expect(audit).to_contain_text("E2E Reviewer")
    expect(audit).to_contain_text("$50")
    with page.expect_download() as log:
        panel(page, "audit-title").get_by_role("button", name="Download audit log (CSV)").click()
    assert log.value.suggested_filename == "taxdiff-audit-log.csv"
    shot(page, "gate-applied")
    assert watch.errors == []
    page.context.close()


def test_notes_and_letters_are_separate_with_copy_and_print(browser):
    page, watch = open_page(browser)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=URL)
    apply_cs_001(page)
    open_tab(page, "Notes and letters")
    notes = panel(page, "notes-title")
    expect(notes).to_contain_text(re.compile(r"\d+ internal notes?, \d+ letters?"))
    # Rewording is counted from each note's own source.
    expect(notes).to_contain_text(re.compile(r"(\d+ of \d+ internal notes reworded by|internal notes? kept (its|their) template)"))
    show = notes.get_by_role("group", name="Show")
    letters = show.get_by_role("button", name=re.compile(r"^Letters \(\d+\)$"))
    internal = show.get_by_role("button", name=re.compile(r"^Notes \(\d+\)$"))
    letters.click()
    expect(letters).to_have_attribute("aria-pressed", "true")
    expect(notes.locator(".note-block.letter").first).to_be_visible()
    expect(notes.locator(".note-block:not(.letter)")).to_have_count(0)
    expect(notes.get_by_role("button", name=re.compile(r"^Print the letter to C-\d{3}$")).first).to_be_visible()
    internal.click()
    expect(notes.locator(".note-block.letter")).to_have_count(0)
    first = notes.locator(".note-block").first
    expect(first).to_contain_text("Internal note")
    first.get_by_role("button", name=re.compile(r"^Copy the internal note for C-\d{3}$")).click()
    expect(first.get_by_role("button", name=re.compile(r"^Copied the internal note for C-\d{3}$"))).to_be_visible()
    shot(page, "notes-and-letters")
    assert watch.errors == []
    page.context.close()


def test_sign_off_by_role_and_undo_the_change(browser):
    page, watch = open_page(browser)
    apply_cs_001(page)
    impact = panel(page, "impact-title")
    expect(impact).to_contain_text("Signed off 0 of 14")
    expect(impact).to_contain_text("Names are typed, not verified")
    accept = impact.get_by_role("button", name="Accept, C-003")
    expect(accept).to_be_disabled()
    impact.get_by_label("Signing as").fill("E2E Partner")
    expect(accept).to_be_enabled()
    accept.click()
    expect(impact.locator("table.worklist tbody tr", has_text="C-003").first).to_contain_text("Accepted by E2E Partner, Preparer")
    # Each route has its role: a partner signs an amendment, the reviewer records a client's choice.
    expect(impact.locator("table.worklist tbody tr", has_text="C-024").first).to_contain_text("Partner sign-off")
    impact.get_by_role("button", name="Hold, C-024").click()
    expect(impact.locator("table.worklist tbody tr", has_text="C-033").first).to_contain_text("Client's choice, recorded by the reviewer")
    impact.get_by_role("button", name="Client declined, C-033").click()
    expect(impact).to_contain_text("Signed off 3 of 14")
    # The count is also a bar that fills; the words are what is read out.
    expect(impact.locator(".signoff-bar")).to_have_attribute("aria-hidden", "true")
    open_tab(page, "Audit log")
    audit = page.locator("table.audit")
    expect(audit).to_contain_text("signed off")
    expect(audit).to_contain_text("E2E Partner, Preparer")
    # The sign-offs are kept with the session; the page opens on the impact list again.
    page.reload(wait_until="networkidle")
    expect(panel(page, "impact-title")).to_contain_text("Signed off 3 of 14")
    # Undo the change: an in-app confirmation lists what it removes, and focus starts on Cancel.
    page.locator("#decision").get_by_role("button", name="Undo CS-001").click()
    dialog = page.get_by_role("alertdialog", name="Undo CS-001?")
    expect(dialog).to_contain_text("Closes 3 sign-offs")
    expect(dialog.get_by_role("button", name="Cancel")).to_be_focused()
    dialog.get_by_role("button", name="Undo CS-001").click()
    expect(dialog).to_have_count(0)
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    expect(page.locator("table.audit")).to_contain_text("undone")
    expect(page.locator(".ask .notice")).to_contain_text("CS-001 was undone")
    expect(chip(page, "Why did C-031's tax go down?")).to_contain_text("Run CS-001 first")
    assert watch.errors == []
    page.context.close()


def test_reset_asks_in_the_page_and_lists_what_it_undoes(browser):
    page, watch = open_page(browser)
    apply_cs_001(page)
    reset = page.locator(".topbar").get_by_role("button", name="Reset", exact=True)
    reset.click()
    dialog = page.get_by_role("alertdialog", name="Reset the session?")
    expect(dialog).to_contain_text("CS-001 (approved by E2E Reviewer")
    expect(dialog).to_contain_text("Run history and the audit log stay")
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_have_count(0)
    expect(page.locator(".scope")).to_contain_text("version 2")
    reset.click()
    page.get_by_role("alertdialog").get_by_role("button", name="Reset the session").click()
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    expect(page.locator("table.audit")).to_contain_text("session reset")
    assert watch.errors == []
    page.context.close()


def test_the_amend_threshold_is_a_firm_setting(browser):
    page, watch = open_page(browser)
    # The question form has no threshold input; the firm setting is edited in About, with a confirmation.
    expect(page.locator(".ask #materiality")).to_have_count(0)
    page.locator(".ask").get_by_role("button", name="Change the amend threshold, a firm setting").click()
    about = page.get_by_role("dialog", name="About this prototype")
    edit = about.get_by_role("button", name="Edit the amend threshold")
    expect(edit).to_be_focused()
    edit.click()
    field = about.get_by_label("Amend threshold ($)")
    field.fill("-5")
    about.get_by_role("button", name="Save").click()
    expect(about).to_contain_text("Enter an amount from $0 to $1,000,000")
    field.fill("100")
    about.get_by_role("button", name="Save").click()
    expect(about).to_contain_text("Change the amend threshold from $50 to $100?")
    about.get_by_role("button", name="Confirm").click()
    expect(about).to_contain_text("Amend threshold (materiality) $100")
    page.keyboard.press("Escape")
    expect(about).to_have_count(0)
    expect(page.locator(".ask .firm-line")).to_contain_text("$100")
    expect(page.locator("table.audit")).to_contain_text("amend threshold changed")
    run_example(page, CS_001)
    shown(page, "waiting")
    expect(page.locator("#approval")).to_contain_text("$100 (firm setting)")
    assert watch.errors == []
    page.context.close()


def test_a_client_fact_can_be_changed_from_the_form(browser):
    page, watch = open_page(browser)
    ask = page.locator(".ask")
    ask.get_by_text("Change a client fact", exact=True).click()
    form = ask.locator("fieldset.fact-row").first
    # A joint return offers the spouse; a single return the taxpayer only.
    form.get_by_label("Client", exact=True).select_option("C-005")
    expect(form.get_by_label("Person", exact=True).locator("option")).to_have_count(2)
    form.get_by_label("Client", exact=True).select_option("C-019")
    expect(form.get_by_label("Person", exact=True).locator("option")).to_have_count(1)
    form.get_by_label("Fact", exact=True).select_option("ot_flsa_hours")
    form.get_by_label("New value", exact=True).fill("abc")
    ask.get_by_role("button", name="Preview", exact=True).click()
    expect(form).to_contain_text("Enter a number of zero or more")
    form.get_by_label("New value", exact=True).fill("150")
    ask.get_by_role("button", name="Preview", exact=True).click()
    # The form writes one instruction per line into the question box, the words the API reads, and runs it.
    expect(page.get_by_role("textbox", name="Question or change")).to_have_value("set C-019 people[0].ot_flsa_hours to 150")
    settled(page)
    shown(page, "waiting")
    gate = page.locator("#approval")
    expect(gate).to_contain_text("Reviewer approval.")
    expect(gate).to_contain_text("C-019")
    expect(gate).not_to_contain_text("empty to")
    # Two facts at once: one line each.
    ask.get_by_role("button", name="Add another fact").click()
    second = ask.locator("fieldset.fact-row").nth(1)
    second.get_by_label("Fact", exact=True).select_option("ot_regular_rate")
    second.get_by_label("New value", exact=True).fill("$30")
    ask.get_by_role("button", name="Preview", exact=True).click()
    expect(page.get_by_role("textbox", name="Question or change")).to_have_value(
        "set C-019 people[0].ot_flsa_hours to 150\nset C-019 people[0].ot_regular_rate to 30")
    settled(page)
    shown(page, "waiting")
    assert watch.errors == []
    page.context.close()


def test_an_example_that_needs_cs_001_offers_it_first(browser):
    page, watch = open_page(browser)
    label = "Why did C-031's tax go down?"
    expect(chip(page, label)).to_contain_text("Run CS-001 first")
    chip(page, label).click()
    offer = page.locator(".offer")
    expect(offer).to_contain_text("is about the returns after CS-001")
    run_first = offer.get_by_role("button", name="Run CS-001 first")
    expect(run_first).to_be_focused()
    # Nothing ran: the click asked first.
    expect(page.locator("[data-run-status]")).to_have_attribute("data-run-status", "none")
    run_first.click()
    settled(page)
    shown(page, "waiting")
    approve(page)
    run_example(page, label)
    shown(page, "answered")
    assert watch.errors == []
    page.context.close()


def test_a_what_if_is_a_preview_only(browser):
    page, watch = open_page(browser)
    run_example(page, WHAT_IF)
    shown(page, "what_if")
    expect(page.locator("[data-run-status]")).to_have_attribute("data-run-status", "what_if")
    expect(page.locator("#approval")).to_have_count(0)
    show_steps(page)
    expect(node(page, "Reviewer approval")).to_have_attribute("data-state", "skipped")
    expect(node(page, "Write notes")).to_have_attribute("data-state", "skipped")
    banner = page.get_by_role("note").filter(has_text="What-if")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("nothing was applied. The rules and the returns are unchanged.")
    # A what-if takes no decision: the guide stops at who it affects.
    expect(page.locator("#guide [aria-current='step']")).to_contain_text("Who it affects")
    expect(page.locator(".reach-count")).to_contain_text(re.compile(r"\d+\s*of 40"))
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    page.get_by_role("tab", name=re.compile(r"^History")).click()
    expect(page.locator("#pane-history .history")).to_contain_text("What-if, nothing applied")
    shot(page, "what-if")
    page.get_by_role("tab", name="Question or change").click()
    # The what-if left nothing in the session: CS-001 still previews against rules version 1 and reaches 15.
    run_example(page, CS_001)
    shown(page, "waiting")
    expect(page.get_by_role("note").filter(has_text="What-if")).to_have_count(0)
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    expect(page.locator(".reach-count")).to_contain_text("15")
    assert watch.errors == []
    page.context.close()


def test_a_step_drawer_shows_its_input_and_output(browser):
    page, watch = open_page(browser)
    run_example(page, CS_001)
    shown(page, "waiting")
    show_steps(page)
    node(page, "Recompute returns").click()
    drawer = page.get_by_role("dialog", name="Recompute returns step")
    expect(drawer).to_be_visible()
    expect(drawer.locator("pre.json")).to_have_count(2)
    expect(drawer.locator("pre.json").nth(1)).to_contain_text('"reached"')
    expect(drawer).to_contain_text("Measured time")
    shot(page, "drawer-engine-a")
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    node(page, "Read the question").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("dialog", name="Read the question step")).to_be_visible()
    assert watch.errors == []
    page.context.close()


def test_cs_900_fails_validation_with_errors(browser):
    page, watch = open_page(browser)
    run_example(page, "Refuse a negative cap with no citation")
    shown(page, "rejected")
    expect(page.locator("#answer-title")).to_be_focused()
    expect(page.locator(".graph-panel .run-status")).to_contain_text("Failed validation")
    show_steps(page)
    expect(node(page, "Validate")).to_have_attribute("data-state", "failed")
    expect(node(page, "Recompute returns")).to_have_attribute("data-state", "skipped")
    answer = page.locator(".answer-card")
    expect(answer.locator(".answer-headline")).to_have_text("CS-900 failed validation. Nothing changed.")
    expect(answer).to_contain_text("must be zero or more")
    expect(answer).to_contain_text("a citation is required")
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    # A change that failed validation has no reach to show.
    expect(page.locator(".reach-strip")).to_have_count(0)
    assert watch.errors == []
    page.context.close()


def test_a_row_opens_its_computation_and_provenance_chain(browser):
    page, watch = open_page(browser)
    run_example(page, CS_001)
    shown(page, "waiting")
    row = page.locator("table.worklist tbody tr", has_text="C-003").first
    expect(row.locator(".method").first).to_be_visible()
    row.click()
    drawer = page.get_by_role("dialog", name="Return C-003")
    expect(drawer).to_be_visible()
    expect(drawer.locator("table.sheet").first).to_be_visible()
    drawer.get_by_text("Inputs", exact=True).click()
    expect(drawer.locator(".chain").first).to_be_visible()
    expect(drawer).to_contain_text("qualified overtime definition")
    expect(drawer.locator('.reads li[data-touched="true"]')).not_to_have_count(0)
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    # A filed return also shows its corrected-return preview in Form 1040-X columns.
    page.get_by_role("button", name="Open return C-024").first.click()
    filed = page.get_by_role("dialog", name="Return C-024")
    expect(filed).to_contain_text("Corrected return preview")
    expect(filed).to_contain_text("A. As filed")
    expect(filed).to_contain_text("Vs. the filed return")
    expect(filed).not_to_contain_text("Engine A")
    assert watch.errors == []
    page.context.close()


def test_phone_width_has_no_horizontal_scroll(browser):
    page, watch = open_page(browser, width=375, height=812)
    # The opening band: heading with Hide this beside it, one sentence and the button, under 250 px.
    band = page.locator(".problem")
    box = band.bounding_box()
    assert box and box["height"] <= 250, box
    head = page.locator("#problem-title").bounding_box()
    hide = band.get_by_role("button", name="Hide this").bounding_box()
    assert head and hide and hide["y"] < head["y"] + head["height"], (head, hide)
    # Before a run the steps are folded; opened, a phone shows them as a list rather than the graph.
    page.locator(".graph-panel").get_by_role("button", name="Show steps").click()
    expect(page.locator(".stepper")).to_be_visible()
    page.locator(".graph-panel").get_by_role("button", name="Hide steps").click()
    expect(page.locator(".synthetic")).to_be_visible()
    expect(page.locator(".scope")).to_contain_text("Rules: 2025, version 1")
    expect(page.get_by_role("button", name="About", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=re.compile(r"^Services"))).to_be_visible()
    run_example(page, CS_001)
    shown(page, "waiting")
    width = page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 375, f"the page is {width}px wide at 375px"
    # While a preview waits, the result reads story, reach, approval, and the approval comes before the long tables.
    gate = page.locator("#approval").bounding_box()
    impact = panel(page, "impact-title").bounding_box()
    assert gate and impact and gate["y"] < impact["y"], "the approval panel should come before the impact list"
    # The name field and both buttons stay at the foot of the screen while the card is in view.
    page.locator("#gate-title").evaluate("(e) => e.scrollIntoView({ block: 'start' })")
    bar = page.locator("#approval .gate-bar")
    expect(bar.get_by_role("button", name="Approve and apply")).to_be_in_viewport()
    rect = bar.bounding_box()
    assert rect and rect["height"] <= 80 and rect["y"] + rect["height"] <= 813, rect
    # Each amount on a phone row carries its label.
    expect(panel(page, "impact-title").locator("td.amounts .cell-label").first).to_be_visible()
    shot(page, "phone-375")
    assert watch.errors == []
    page.context.close()


def test_the_status_page_stacks_at_phone_width(browser):
    page, watch = open_page(browser, width=375, height=812, path="/status")
    expect(page.get_by_role("button", name="Check again")).to_be_enabled(timeout=120_000)
    width = page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 375, f"the status page is {width}px wide at 375px"
    # Each row stacks into one column, and no endpoint breaks inside its path.
    tracks = page.evaluate("() => getComputedStyle(document.querySelector('table.status-table tbody tr')).gridTemplateColumns")
    assert len(tracks.split()) == 1, tracks
    tallest = page.evaluate("() => Math.max(...[...document.querySelectorAll('.status-table .endpoint')].map((e) => e.getBoundingClientRect().height))")
    assert tallest < 26, f"an endpoint wraps: {tallest}px tall"
    services = page.locator('section[aria-labelledby="services-title"]')
    expect(services).not_to_contain_text("after 4 s")
    expect(services).not_to_contain_text("release api.policyengine.org")
    expect(services).to_contain_text("PolicyEngine-US")
    shot(page, "status-375")
    assert watch.errors == []
    page.context.close()


def test_reduced_motion_draws_without_animation(browser):
    page, watch = open_page(browser, reduced=True)
    expect(page.locator(".graph-caption")).to_contain_text("Reduced motion is on")
    run_example(page, "Refuse a negative cap with no citation")
    shown(page, "rejected")
    show_steps(page)
    duration = page.evaluate("() => getComputedStyle(document.querySelector('.lane')).transitionDuration")
    assert duration in ("1e-06s", "0s", "0.000001s"), duration
    # A result arrives in its final state: its story, squares and rows are drawn at once and nothing moves.
    run_example(page, CS_001)
    shown(page, "waiting")
    held = page.evaluate("() => [...document.querySelectorAll('[data-arrive], table.worklist tbody tr')].filter((e) => getComputedStyle(e).opacity !== '1').length")
    assert held == 0, held
    moving = page.evaluate("() => document.getAnimations().filter((a) => a.playState === 'running').length")
    assert moving == 0, moving
    assert watch.errors == []
    page.context.close()


def test_a_result_below_the_fold_is_drawn_without_scrolling(browser):
    # The impact list sits below the fold while the preview waits; its rows are drawn still, never left hidden
    # until someone scrolls to them, so a full-page capture or a printout shows every row.
    page, watch = open_page(browser)
    run_example(page, CS_001)
    shown(page, "waiting")
    expect(page.locator("table.worklist tbody tr").first).to_be_attached()
    box = page.locator("table.worklist").bounding_box()
    assert box and box["y"] > 900, f"the impact list should start below the fold: {box}"
    page.wait_for_function("() => [...document.querySelectorAll('table.worklist tbody tr')].every((e) => getComputedStyle(e).opacity === '1')",
                           timeout=3_000)
    assert watch.errors == []
    page.context.close()


def test_an_unsupported_question_says_what_it_can_do(browser):
    page, watch = open_page(browser)
    page.get_by_role("textbox", name="Question or change").fill("What is the weather in Irvine?")
    page.get_by_role("button", name="Run", exact=True).click()
    shown(page, "unsupported")
    expect(page.locator(".graph-panel .run-status")).to_contain_text("Not run: question not recognized")
    expect(page.locator(".answer-headline")).to_have_text("This prototype cannot run that question.")
    expect(page.locator("#answer-title")).to_be_focused()
    page.locator(".answer-card").get_by_role("button", name="Where does C-017's overtime deduction come from?").click()
    shown(page, "answered")
    assert watch.errors == []
    page.context.close()
