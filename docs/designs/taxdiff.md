# TaxDiff: change-impact triage for prepared tax returns

Author: Neric Joel A (neric-joel) · Status: draft · Date: 2026-11-02

--------------------------------------------------------------------------------

## Overview

TaxDiff is a review tool for a tax practice, or for an AI tax preparer, that works out what a change to
tax law or client facts does to returns already prepared. A change arrives as a change set: a rule edit (a
cap, a threshold, or the definition of a deduction), a fact edit (a client's wages or birth date), or a new
document (an employer overtime statement) read into fact edits. For each change set the tool recomputes the
book, lists the returns the change reaches, shows the before and after value of every line on each, and
routes each reached return to its next step. Any other return that moved stops the change. PolicyEngine-US,
an open-source model of the US tax code, recomputes the same returns as a second engine. A person sees the
routes and amounts in a preview, and approval applies exactly that preview.

The prototype computes part of the tax year 2025 federal Form 1040: wages, the standard deduction, the three
Schedule 1-A deductions added by Pub. L. 119-21 (the One Big Beautiful Bill Act: qualified tips, qualified
overtime and the enhanced senior deduction), taxable income, and regular tax. Non-goals lists what it leaves
out. It runs on a synthetic book of 40 returns (`data/book_ty2025.json`). The code is in `src/taxdiff/`,
the review screen in `app/streamlit_app.py`, and the command line in `src/taxdiff/cli.py`.

The prototype targets tax platforms for CPA firms in which AI prepares returns and firm staff review them.
Each step of this tool's pipeline is a lane, and the lanes correspond to pieces such a platform has:

| Lane | Platform piece it corresponds to |
|---|---|
| Intake, Documents | Organizer and document extraction |
| Engine A, Engine B | Tax computation |
| Route, Notes | Review by firm staff |

### Terms

| Term | Meaning |
|---|---|
| Book | The returns a practice has for one tax year, in any status: prep, review or filed |
| Rule pack | The tax parameters for one tax year (`rules/ty2025.yaml`), each with a citation. It also names the definition of qualified overtime in force. Each applied rule change makes a new version: rule pack v1, v2, and so on |
| Change set | One change to apply: rule edits, fact edits, or a document read into fact edits |
| Reached | A return whose computed lines read a rule or fact the change set touches. A reached return can end with no line changed |
| Preview | A run of every lane up to approval: it computes every route and amount and applies nothing |
| Unknown | A line value that cannot be computed until a missing fact arrives |
| Mixed overtime | Overtime pay that combines overtime the Fair Labor Standards Act (FLSA) requires with overtime required only by state law or a contract, with no split given |
| Engine A, Engine B | Engine A is this project's engine and the engine of record. Engine B is PolicyEngine-US |
| Checkable | A return Engine B can compute: every person has a valid SSN, and no fact the overtime premium needs is Unknown |
| As filed | The line values a filed return was filed with, stored with the return |
| Drift | A filed return that no longer recomputes to its as-filed amounts at the start of a session |
| Lane | One pipeline step with an input, an output, a state (done, failed, skipped or waiting) and a measured duration |
| Lane board | The screen section that shows every lane of the latest run |

## Problem

A return is computed from law and facts that keep changing after preparation. In the 2026 filing season:

- The IRS issued its overtime guidance in stages: Notice 2025-69 on 2025-11-21; FAQs in Fact Sheet
  2026-01 on 2026-01-23, replaced by FS-2026-13 in August 2026; and Schedule 1-A instructions, reported
  published on 2026-03-04 (FOX 13).
- About 21 million filers claimed the new overtime deduction before Tax Day (Associated Press,
  2026-04-15). Only the overtime premium the FLSA requires counts, and employers did not have to report
  that amount separately for 2025 (Notice 2025-62). A return that deducts all the overtime pay in W-2 box
  14 overstates the deduction, and analysts expected such claims (Bangor Daily News, 2026-04-17).
- Arizona HB 4168, signed 2026-06-13, added state subtractions for tips and overtime from tax year 2025,
  which reaches returns already filed.

The gap this design fills is a list, for one change, of the prepared returns it reaches and how much each
one moves. Without that list a reviewer reopens returns one at a time, or relies on memory to find the
filed returns that may need an amendment. An AI preparer has the same gap each time it changes how its
engine applies the law. Whether existing preparation software already produces such a list across a book
is an open question (Q5), and so are the number of returns in progress when a change lands and the time a
manual re-review takes.

The design records, for every computed line, which rules and facts it read. A change can only move a
return that read the changed rule or fact, so after recomputing the book the tool knows which returns the
change reaches and why, diffs and routes them, and treats any other return that moved as an error.

## Goals

| Goal | Statement | Checked by |
|---|---|---|
| G1 Reach | For every change set that passes validation, list every reached return with the before and after value of each line and the change in tax. If any return outside that list moved, the change is not applied. Filed returns keep their as-filed amounts, and a session starts by listing any drift, which shows an engine release or a starting rule pack that no change set describes | Tests that change each of the 29 rule parameters and make 60 random fact edits; drift tests |
| G2 Routing | Route every reached return by the routing table in Route. The Engine A and Route lanes together take under one second for the 40-return book | Routing table tests; a timing test |
| G3 Approval and audit | Reject an invalid change set with a reason per field and keep the last valid version. Show the routes and amounts in a preview, and apply exactly that preview when a named approver approves it. Give every outcome an audit event, including an instruction the intake lane refuses | Scenario 3 tests; preview, approval and audit tests |
| G4 Parity | Compare Engine A with Engine B, a separate implementation of the arithmetic, on every checkable return, and report agree, differs or not checked | Cached parity tests |
| G5 Traceability | Every rule parameter carries a citation. Every figure in a template note or a document request is inserted by code from Engine A's results, and a reworded reviewer note is kept only when it keeps every figure, the client and the route | Rule pack validation; note check tests |
| G6 Cost | Run with no paid service and no real taxpayer data; every LLM feature is optional and has a deterministic fallback | The test suite runs with no key and no network |
| G7 Visible lanes | Show each lane's state and measured duration on the screen as the run happens, with Engine B and Law running side by side. Where an open-source tool or public source fits a lane, use it: Docling reads PDFs, the Electronic Code of Federal Regulations (eCFR) supplies regulation text, PolicyEngine-US is Engine B. An optional LLM can draft a change set from text and reword internal reviewer notes, behind the checks in Intake and Notes. Engine B, Law and note rewording can be off or fail without stopping the change; when Docling or an LLM cannot read an input, that input is refused with a reason and nothing else changes | Screen tests; lane failure tests |

## Non-goals

| Non-goal | Detail |
|---|---|
| A complete Form 1040 | AMT, capital-gain rates, credits, QBI, SALT, car-loan interest and state returns are not modeled, and the screen says so. Also not modeled for the standard deduction: blindness, a dependent's limited amount, and the zero amount when a separately filing spouse itemizes |
| E-file, a client portal, user accounts or a database | State lives in one browser session or one command run |
| Deciding how the law should be read | The rule pack selects a definition, and a person approves every change |
| Undoing one approved change | Reset returns the session to rule pack v1 and the original facts, and keeps the audit log |

## Open questions

| ID | Question |
|---|---|
| Q1 Frequency | How often do mid-season changes happen in a practice, and what share of the book does a typical one reach? A small share would argue for building this into existing software rather than as a separate tool |
| Q2 Trust | Will reviewers accept a "not reached" list, or re-check those returns anyway? |
| Q3 Effective dates | Arizona HB 4168 changed 2025 after many 2025 returns were filed. Should a rule pack version carry the date it took effect, so the tool can tell a return filed under the old law from one prepared under the new law? Each version now applies to the whole year, and the filed status alone drives routing |
| Q4 Hosting | Answered: the web dashboard and its API are one Vercel project with two services (Web dashboard and API). The Streamlit screen stays a local tool |
| Q5 Existing tools | Does desktop preparation software, or MiraFlores's own engine, already list the returns a change reaches across a book? |

## User scenarios

### Scenario 1: a rule-interpretation fix reaches only the overtime returns

- Given: the book of 40 returns computed under rule pack v1, which counts all W-2 box 14 overtime pay as
  qualified overtime. PolicyEngine-US results are cached.
- When: a reviewer selects change set CS-001 (count only the FLSA premium, per Notice 2025-69), previews
  it, enters their name, and approves it.
- Then: the preview shows 15 reached returns and their routes: Update return 3; Back to reviewer 4;
  Amendment review 5 (one also waits for a document); Document request 1; Too small to amend 1; No action
  1. The full recompute reports 0 unexpected changes. Approval applies that preview unchanged and writes the
  notes, with a client notice for each filed return it reaches. The cached PolicyEngine-US release agrees
  on all 40 returns under rule pack v1 and on the 38 it can check after CS-001; the current release behind
  the PolicyEngine API differs on C-010 and C-021 (see Engine B).

### Scenario 2: a document resolves a missing fact

- Given: scenario 1 has run. Client C-017 (filed) has mixed overtime, so its overtime line is Unknown and
  it waits for a document.
- When: the reviewer previews and approves CS-002 (the employer statement as text) or CS-004 (the same
  statement as a PDF).
- Then: the Documents lane reads 160 FLSA overtime hours, a regular rate of $24.00 and a premium of
  $1,920.00, and checks the tax year and the SSN's last four digits. Only C-017 is reached. It routes to
  Amendment review, $1,151 above the tax on the return as filed. For the PDF, the lane names the method:
  Docling, or Docling's cached conversion.

### Scenario 3: invalid input is rejected

- Given: any state of the book.
- When: the reviewer runs CS-900 (a negative cap with no citation) or CS-901 (a statement for tax year
  2024).
- Then: the change is rejected with one reason per field, the rule pack and facts stay at their last
  valid version, and the audit log records the rejection.

### Scenario 4: a written instruction becomes a change set

- Given: the reviewer types `set overtime.cap.other to 2500 per Hypothetical change`, or pastes a
  paragraph of guidance and chooses an LLM provider.
- When: the reviewer selects "Read instruction".
- Then: the pattern parser, or the LLM, proposes a change set, and the screen shows it. An LLM proposal is
  refused, and the refusal audited, unless every number, client and citation in it appears in the pasted
  text. The proposal then goes through the same validation, preview and approval as any other change set.
  Neither parser computes a tax amount.

### Scenario 5: Engine B runs live on a local machine

- Given: PolicyEngine-US is installed (`pip install -e ".[parity]"`).
- When: the reviewer chooses "Run live" and previews a change.
- Then: Engine B computes every checkable return in one batch and writes the results to the cache, and
  the lane board shows each lane and its measured duration as it finishes. Approval applies the preview
  without running PolicyEngine-US again.

## Technical plan

### Components and data flow

```text
 written text ──────────► Intake ─────┐
 document change set ───► Documents ──┼──► Validate ──► Engine A ──┬──► Engine B ──┬──► Route ──► Approve ──► Notes
 change set file ─────────────────────┘                           └──► Law ───────┘
```

Lanes run in the order their inputs allow. Engine A runs first because it can stop the change, and then
nothing else needs to run. Engine B and Law each read the validated next rule pack and facts and do not
depend on one another, so they run side by side in two threads; Route waits for Engine B. Every lane up to
Route is a preview that changes nothing. Approve applies the preview's stored result as it stands, without
recomputing, and Notes then writes the notes. Each lane reports its result through a callback on the
caller's thread as soon as it finishes, so the screen can show the run while it happens. Every outcome
records one audit event.

| Lane | Input | Output | On failure |
|---|---|---|---|
| Intake | Instruction text | Proposed change set | Refused with a reason and audited; nothing runs |
| Documents | Employer overtime statement, text or PDF | Fact edits with the fields read | Change rejected with a reason |
| Validate | Change set, current rule pack and facts | Next rule pack and facts, or field errors | Change rejected; the last valid version stays |
| Engine A | Next rule pack and facts | The book recomputed; the reached returns with line diffs; any unexpected change | Change stopped; Engine B, Law and Route are skipped |
| Engine B | Next rule pack and facts | PolicyEngine-US outputs per checkable return | Every return not checked; the run goes on |
| Law | Changed rule keys | Authorities with links and dated eCFR excerpts | No authorities attached; the run goes on |
| Route | Line diffs, parity, materiality | A route per reached return | Change stopped |
| Approve | A preview and the approver's name | The preview applied as the next state | Nothing applied; a blank name is refused |
| Notes | Routed returns | Reviewer notes and document requests | Template text |

Any error outside Engine B and Law stops the change, keeps the last valid version and records an error
event, so every run ends previewed, failed validation, stopped, not recognized or error, and every
decision ends applied, turned down or refused.

### Data model

| Type | Module | Holds |
|---|---|---|
| RulePack | `rules.py` | Name, tax year, version, and per parameter key a value, citation and URL. A schema lists every key the engines read and its kind: money, positive, rate, date, rate schedule, or a named definition. Loading or editing a rule pack validates every value |
| ClientReturn, Person | `facts.py` | Status, filing status, facts version, and per person: birth date, the SSN's last four digits and its validity, wages, overtime evidence (box 14 pay, pay multiple, FLSA basis, reported premium, FLSA hours, regular rate) and tips. A filed return also stores its as-filed line values, a record that change sets cannot edit |
| Book | `facts.py` | Tax year and returns; loading refuses a filed return without as-filed values |
| ChangeSet | `changes.py` | Id, kind (rule, fact or document), title, author, rationale, rule edits, fact edits, or a document path and client |
| ComputedReturn | `engine.py` | The value of every line and, per line, the set of paths it read |
| ReturnImpact, ImpactReport | `impact.py` | Before and after results, line diffs, tax change, change against the return as filed, route; the not-reached and unexpected lists |
| State | `workflow.py` | Rule pack, book, results, as-filed amounts, drift, parity and audit log |
| Run | `workflow.py` | One run's lanes, validation, report, authorities and notes and, for a preview, the state approval applies |
| AuditEvent | `workflow.py` | One outcome and the context it ran in (Approve, preview and the audit log) |

### Intake

The pattern parser reads one instruction per line, in the form `set <rule key> to <value> per <citation>`
for a rule edit or `set <client id> <fact path> to <value>` for a fact edit, for example
`set C-019 people[0].ot_flsa_hours to 150`. Rule edits and fact edits go in separate change sets, and
validation requires the citation.

An LLM can read free text instead. The reviewer chooses one of two providers: the paid Anthropic API, or a
local Ollama model that needs no key and keeps the text on the machine. The guarantee is that every number, client
and citation in a proposal appears in the text the model read; otherwise the proposal is refused and the
refusal is audited. The approver checks what this cannot: values that are not numbers (a definition name,
a date) and the choice of parameter key. The preview helps with the key, because a wrong key reaches a
different set of returns.

### Documents

The Documents lane reads one employer overtime statement for one client. A pattern map looks for the tax
year, the employee name, the SSN's last four digits, FLSA overtime hours, the regular rate and the FLSA
premium; each is read or left empty, and nothing is guessed. The lane rejects the statement when the
client is not in the book, the tax year differs from the book's, the SSN digits match no one on the
return, or no overtime field is found. The fields read become fact edits on the matching person, and the
change then follows the fact-edit path. A PDF goes through Docling first. Each conversion is cached by the
PDF's SHA-256, so a machine without Docling reads any PDF converted once; "Convert PDF statements again
with Docling" needs Docling installed and never falls back to the cache.

### Validate

Validate builds the next rule pack and facts from the change set, or collects every reason it cannot: an
unknown key, a value of the wrong kind, a missing citation, an unknown client or fact path, or an edit to
a return's status or as-filed amounts. A change set that edits several fields of one client makes one new
facts version.

### Provenance and reach

Engine A computes lines in a fixed order. Each line records what it reads, as paths: `rule:<key>` for a
rule pack parameter, `fact:<path>` for a fact (including a fact whose value is empty), and
`code:<definition>@<version>` for the overtime definition it called. A line that uses an earlier line
takes that line's reads too, so the tax line carries every read below it. Engine code must read every
input through the tracker: a value read any other way is invisible to the reached list.

A rule edit touches `rule:<key>` for every client; a fact edit touches `fact:<path>` for one client. A
return is reached when its reads before the change include a touched path or a path below it. Engine A
recomputes the whole book once per change. The reached returns are diffed, routed and explained by their
reads; every other return must be unchanged, or the change stops and the audit log records the returns
that moved. A passing check shows that this change moved no other return in this book; it does not show
that the reads are complete for other books or changes. The `code:` reads show which definition a line
used; no change set touches them. An AI preparer's change in how it applies the law is modeled as a rule
edit that selects a new definition, as CS-001 does; a change to shared code shows up as drift.

### Engine A

Engine A is deterministic Python with amounts in cents. It computes wages, the standard deduction with the
age 65 amount, the three Schedule 1-A deductions with their caps, phase-outs and filing-status rules,
taxable income, and regular tax from the rate schedules in Rev. Proc. 2024-40: by the IRS Tax Table below
$100,000 of taxable income (rate-schedule tax at the row's midpoint, rounded to the dollar) and by the Tax
Computation Worksheet at or above it. That Tax Table rule matches all 8,248 cells of the published 2025
table, and tests pin 17 published values. Values and citations live in `rules/ty2025.yaml`; the overtime
definitions live in `provisions.py`. The `flsa_premium` definition applies Notice 2025-69 and takes the
first rule that fits:

1. The premium the employer reported.
2. Zero, when the overtime is required only by state law or a contract.
3. For FLSA overtime, one third of the overtime pay at 1.5 times the regular rate, or one fourth at 2
   times.
4. Half the regular rate times the FLSA overtime hours.
5. Otherwise Unknown.

### Engine B

Engine B is PolicyEngine-US 2.90.4, a separate implementation of the arithmetic. Both engines get the same
facts, the same tax year and the same rule pack:

- PolicyEngine-US takes the qualified overtime premium as an input. The adapter derives it with the
  definition the rule pack selects, using Engine A's provision code, so Engine B does not check the premium
  itself; tests pinned to Notice 2025-69 do.
- PolicyEngine-US holds current law in its own parameters, and rule pack v1 holds the same values. When a
  rule pack value differs from rule pack v1 for a mapped parameter (the overtime caps and phase-out starts,
  the tips cap and phase-out starts, the senior amount), the adapter passes the new value to
  PolicyEngine-US as a reform. A changed parameter outside that set (a standard deduction amount, a rate
  schedule, a phase-out step, the senior phase-out, the Tax Table limit) cannot be passed, so every return
  that reads it is not checked while its value differs from rule pack v1.

A difference therefore means the two implementations disagree on the arithmetic for the same inputs. The
lines compared are the standard deduction, the three Schedule 1-A deductions, taxable income and
rate-schedule tax; PolicyEngine-US does not use the Tax Table. The tolerance is $0.50: PolicyEngine-US
computes in 32-bit floats, and the largest difference measured was under $0.01. A return with an invalid
SSN is not checked, because the adapter does not pass SSN eligibility.

Results are cached in `cache/policyengine.json`, keyed by each return's inputs and the reform, because a
batch with a reform takes about 26 seconds. A cache holds results from one PolicyEngine-US release: a live
run under another release starts the cache again, and the lane summary and the audit event name the
release. A return with no cached result for its inputs is not checked, and routes by its status alone. The
screen header names the overtime definition in force, so a pack that departs from current law shows there;
previewing CS-001 shows what correcting it reaches.

### Law

The Law lane attaches the authorities behind each changed rule key, by key prefix. An overtime key gets IRC
225, Notice 2025-69, Schedule 1-A and the FLSA regulations 29 CFR 778.107 and 778.110; the tips, senior,
standard deduction, age, rate schedule and Tax Table keys get their own. The screen lists them under the
impact list. A fact change gets none from this lane; the return detail shows the authorities behind each
line instead. Each FLSA entry carries the first paragraph of its section, copied from eCFR XML saved for
2026-10-29, so the excerpt is traceable to its source and date; the lane makes no network call. With the
saved excerpts missing or unreadable, the FLSA entries keep their links and lose their excerpts.

### Route

The Route lane routes every reached return, including reached returns with no line change. It takes the
line diffs, the change in tax against the return as filed, the parity result and the firm's materiality
setting ($50 by default). Rows are checked in order and the first match wins:

| Order | Condition | Route |
|---|---|---|
| 1 | A fact a line needs is Unknown | Document request; for a filed return, Amendment review, with the request written too |
| 2 | No line changed | No action |
| 3 | Engine A and Engine B disagree after the change | Back to reviewer |
| 4 | Status prep | Update return |
| 5 | Status review | Back to reviewer |
| 6 | Status filed, and the change against the filed tax is smaller than materiality in either direction | Too small to amend: no amended return, and the client is told |
| 7 | Status filed, otherwise | Amendment review |

Every filed return a change reaches gets a client notice drafted in its note, whatever the amount, since
Circular 230 section 10.21 requires a practitioner to tell the client about an error on a filed return and
sets no minimum. The notice recommends an amended return, or, below materiality, offers one.

Row 3 comes before the status rows because, when the engines disagree, the new amount itself is in
question, and a reviewer checks it before anyone decides on an amendment; the reviewer note still carries
the change against the return as filed. A return Engine B did not check routes by its status alone, and
the Route lane summary says how many reached returns Engine B checked. Row 7 applies in both directions: a
filed return whose tax falls by materiality or more also goes to Amendment review, since an amended return
would claim the refund.

### As-filed amounts

Each filed return stores the line values it was filed with, and a book that lacks them for a filed return
does not load. Route compares a reached filed return's new tax with that stored amount. At the start of a
session the tool recomputes every filed return under the starting rule pack and the current engine and
lists any that no longer match as drift; the screen header and `taxdiff book` show it. Drift appears
when a session starts from a rule pack other than the one a return was filed under (`--rules`), or when an
engine release changes shared arithmetic, which no change set describes. With the shipped book and rule
pack v1 there is none.

### Approve, preview and the audit log

Every run is a preview until approval. "Preview" on the screen, or `taxdiff run` without `--approve`,
stops at the Approve lane: the impact list is labeled "preview, not applied", and only the audit log
changes. "Approve and apply" applies that preview's stored result, with the routes and amounts the
reviewer saw. It is enabled only when the latest run is a preview of the change set on the screen with the
same materiality, Engine B mode and Docling setting, and only once the approver has entered a name. On the
command line, which has no interactive step, `--approve` previews each change and applies it in the same
command; the audit log records both. The approver's name records who approved; it does not check who they
are.

Every outcome records one audit event: previewed, applied, turned down, failed validation, stopped, not
recognized, refused or error. Each event names the change,
the outcome, the reach and route counts or the reasons, the approver, the rule pack's version and a hash
of its parameters and citations, the facts versions a fact change moves, the materiality, Engine B's
release, mode and counts, and the TaxDiff version. The hash lets saved `--json` outputs from two
sessions tell two different "v2" rule packs apart. The audit log lives for one browser session or one
command run; the screen shows it in an expander, and the command line prints it after the runs and
includes it in `--json` output.

### Notes

Notes are templates filled by code with the line diffs and tax changes. Document requests go to clients
and are never reworded. With an LLM provider chosen, internal reviewer notes may be reworded, and a
rewording is kept only if its figures match the template's in the same order and with the same signs, it
still names the client, and it keeps the route phrase word for word. This catches a swapped before and
after value, a tax increase turned into a decrease, and a changed decision. It does not catch a changed
direction word next to the same signed figure, so the option is off by default and the screen shows which
text each note uses.

### Review screen

The screen shows, top to bottom:

| Section | Content |
|---|---|
| Header | Book size and tax year; rule pack version and the overtime definition in force; Engine B counts with the cache date; drift on filed returns; what is not modeled |
| Book | Every return with status, money lines, parity and the route from the last applied change |
| Change | An example change set or a written instruction with its intake provider; approver; materiality; Engine B mode; "Convert PDF statements again with Docling"; reviewer note wording; "Preview", "Approve and apply" and "Reset book" |
| Lanes | The lane board, which updates while a run is in progress and replays the run stage by stage afterwards; Engine A has its own stage, and Engine B and Law share the next |
| Impact | The reached returns of the latest preview or applied change, with route, lines changed, tax change, change against filed, parity and missing facts; then the authorities for the change |
| Return | Form lines with line numbers, before and after values and, for a filed return, its as-filed values; the reads behind a chosen line; authorities with eCFR excerpts; parity differences; the note and the source of its text |
| Audit log | Every audit event |

Styling is measured from IRS Schedule 1-A (Form 1040) 2025: achromatic neutrals and ruled rows. Color marks
routes, lane states, parity, changed lines and the primary button.

### Web dashboard and API

The dashboard (`web/`, Next.js with @xyflow/react) and the API (`src/taxdiff/api/`, FastAPI) are two
services of one Vercel project; `vercel.json` routes `/api/*` to the API and everything else to the
dashboard, so the browser talks to one origin. The API holds no tax logic: it reads a question, runs the
lanes above through `workflow.run_change` and `approve_run`, and streams one NDJSON event per node state
change (`running`, `queued`, `done`, `failed`, `skipped`, `waiting`) with the node's measured time, its
source (live, cached, fallback) and its input and output, then the result. The stream is sent with `Content-Encoding:
identity`, so Vercel's CDN passes it through instead of compressing it (a compressor holds events back until
a block fills), and the dashboard reads it with a stream reader while a clone of the response waits for the
request to complete, so the browser never records a finished run as canceled.

| Node | Job | Fallback |
|---|---|---|
| Question | Replays the approved change sets from rule pack v1 and checks each receipt | A history that does not verify stops the run with the reason |
| Intake | Reads plain English into a change set or a question about a client (`lanes/questions.py`): shipped change set ids and `set` lines by code; other text by the Anthropic API when the server has a key, kept only if every number, client and citation is in the text and every value is one number or word | The pattern reader, with the reason on the node |
| Documents to Merge and route | The lanes of the Technical plan, reported through `on_start` and `on_lane` | As in the lane table |
| Law | Fetches the FLSA sections from the eCFR API (`law_mode="live"`) | The saved excerpts, marked cached with the reason |
| Approve | Stops with the preview; the reviewer approves or turns it down. A what-if skips it: it is previewed, answered and never applied, and no token is signed for it | A preview whose signed token does not match is refused |
| Notes | Templates, reworded by the Anthropic API when available under the note check | Templates |
| Explain | Answers the question from the run (`lanes/explain.py`), with each figure linked to its row; a rewording is kept only if its figures (in order), ids, magnitude words and sources match the template and every figure is in the run's JSON | The template |
| Audit log | The run's audit events, kept in the browser tab | None needed |

The server keeps no session. The browser sends the approved change sets with each request, each with a
receipt the server signed (HMAC with `TAXDIFF_SECRET`) over the preview's digest, the change, the
approver and the previous receipt; a replay re-previews each change and verifies its receipt before it
applies it. Approval needs the preview token the server signed over the preview's digest and the
session's last receipt, and the server recomputes the preview before it applies it. A POST must be JSON
and come from the dashboard's own site; bodies are capped at 64 KB, questions at 2,000 characters and
histories at 25 changes (a 26th approval is refused); each instance runs 8 requests at once and throttles
each address (40 runs and 15 runs with a model per 10 minutes, after which the pattern reader and templates
run). Numbers in a change are refused at 10^12 or more, or with more than 6 decimal places, before any
arithmetic or formatting. Every model call made for a run reads a guard with the run's stop flag and a
deadline 5 s before the stream's 90 s: a cancelled run's queued calls end at once, and no call starts with
under 3 s left. An approval rewords at most 8 reviewer notes; each instance makes at most 300 model calls an
hour and reuses a reply it already has for the same prompt; the deployment's ceiling is the monthly spend
limit on its key. On the host, approvals are refused unless `TAXDIFF_SECRET` is set.

### Design changes (2026-11-02)

Testing the hosted prototype end to end, against a hand recompute of every return from the IRS forms and
against reviewers' own wording, changed the design in these ways:

| Area | Change |
|---|---|
| Engine B | The hosted app asks the public PolicyEngine API (`api.policyengine.org/us/calculate`, no key) for the whole book in one request, with any parameter reform; an answer reused from memory for the same request is labelled Cached, and the cached release stands in, with the reason, when the API does not answer within 5 s, so a preview never waits longer. The current release differs on C-010 (it allows the overtime deduction on a separate return) and C-021 (it reduces the deduction continuously above $150,000, where Schedule 1-A line 19 counts whole $1,000 steps); both show as disagreements, each with its cause and the authority this project's engine follows (IRC 225(e); Schedule 1-A line 19) |
| Law | Live on the host: the eCFR sections (kept an hour per server), a HEAD request to each cited irs.gov and Cornell source, and a Federal Register API search for Treasury and IRS documents on the provision the change touches (for tips, the 2026 final rule on occupations that customarily received tips) |
| Worksheets | Engine A records each Schedule 1-A worksheet step as it computes (lines 14a to 21 for overtime, 6 to 13 for tips, 32 to 37 for seniors) and the Notice 2025-69 method with its arithmetic (for example "Method B: $9,000 of 1.5x overtime pay, divided by 3"). The steps are recorded, never read back into the computation |
| Filed returns | A corrected-return preview (as filed, change, corrected; Form 1040-X columns) for each filed return; a client notice for every filed return reached; a filed return with a missing fact goes to amendment review with its document request |
| Citations | A rule edit's citation must name an authority (IRC section, notice, revenue procedure or ruling, publication, CFR section, public law, form or schedule, or a URL); a citation that starts with "Hypothetical" makes the change a what-if, which the workflow never applies, on the web or the command line. The authority must be of a kind that can set the parameter: an amount the Code fixes (caps, the senior amount, phase-outs, the 2025 standard deduction) only by its own Code section or a public law; an inflation-adjusted amount also by revenue procedure; the overtime definition also by regulation, notice or ruling. An authority the pack marks as superseded for the parameter cannot set it, and P.L. 119-21 counts as the source of the amounts it enacted in IRC 63, 151, 224 and 225. A change whose every value equals the current one is refused as nothing to change. A citation that the pack already cites for the parameter's current value cannot support a different value ("IRC 225 gives the overtime cap ... as $12,500"). On the host, each cited authority with a predictable address (IRS notices, revenue procedures and rulings from 2020 on, publications, Code and CFR sections) is looked up while the change is validated; one that is not there (Rev. Rul. 2025-99: HTTP 404) stops the change, and the approval shows what the lookup found |
| Questions | The code reads first. Its exact readings of a short request (a stated fact, several facts in one sentence, a parameter with an absolute or relative value and its source or a what-if framing, a named client's line, a statement on file, questions about the session) stand without the model. The model reads what the code cannot: pasted passages and free-form wording. Its reading of a pasted passage must quote the sentence that states the change, checked word for word; a change it proposes that fails validation gives way to the code's reading; a what-if's citations are always Hypothetical. A pasted passage is never mapped to a change by a keyword. A statement on file runs as its saved change set, with the same id as on the command line |
| Eval | Three sets: `tests/eval/intake_cases.json` (26 requests written with the reader), `tests/eval/intake_blind.json` (103 written without seeing the reader's code, each with the reason for its expected reading, then used to improve the reader: 77 per run before, 103 after) and `tests/eval/intake_holdout.json` (62 written later by another author: 49 and 50 per run when first measured, 61 after the general rules its misses led to). `scripts/eval_intake.py --record --runs 3` asks the model three times per case and looks up every authority its readings cite; a replay runs every check. A case counts as correct only when it is correct in every recorded run. A wrong reading that would run (a change that validates, or an answer) counts as wrong and accepted: zero in every set and run. The score, its history and the misses are on the Status page |
| Answers | Each answer is a headline and sections built by code (fact rows, lists, paragraphs, every figure linked). No model rewords an answer: changing an outcome ("was not run") or a direction ("went up") would change its meaning without changing a figure, which no figure check catches. A question about a client's Schedule 1-A line is answered with the worksheet steps the engine recorded. The headline leads with what the change does ("CS-001 was applied to 15 of 40 returns: 5 filed returns need amendment review and 2 wait for a document"). Totals are gross: more tax, less tax and waiting on a document, never only a net |
| Missing facts | A return that waits on a document still shows how far its tax can move. Engine A computes it twice more with the missing amount at each end of its range: for overtime with no FLSA split, none of the W-2 box 14 pay, or all of it at the notice's share for its pay multiple (a third of 1.5x pay, a fourth of 2x pay); for tips whose occupation is unconfirmed, none or all. A missing amount only adds to a deduction and the tax never rises as a deduction grows, so the two results bound the tax. C-017 shows +$1,030 to +$1,580 against the return as filed and C-019 +$928 to +$1,632, the same figures a hand computation from the 2025 Tax Table gives. The bounds sit beside the Unknown in the impact list, the answers, the reviewer note and the client notice; the line's value stays Unknown, so nothing is filed or routed on a bound. Without a known pay multiple there is no bound and the change stays unknown |
| Routing | A change that moves no line of a return gives it no new step, even when the return still waits on an earlier request. A new statement or corrected facts that support less qualified overtime, under the Notice's methods, than the return as filed deducted send a filed return to amendment review (a return in progress back to the reviewer), whatever the active rule pack computes: the deduction on a return can never exceed the qualified overtime it is figured from |
| Letters | Internal notes and letters to clients are separate. Every filed return reached gets a notice that says what the error means: interest from the original due date and a possible accuracy-related penalty that an amended return filed before IRS contact can avoid (IRC 6601, IRC 6662, Treas. Reg. 1.6664-2(c)), or the refund window (IRC 6511(a)). A preview carries its letters, so they are read before approval; letters are never reworded |
| Notice methods | Method C: an overtime premium paid above the regular rate at more than 1.5x counts at the fraction that leaves the FLSA half (one half at 2x; the notice's example 3, $10,000 to $5,000), as the fact `ot_premium_paid` |

### Command line

| Command | What it does |
|---|---|
| `taxdiff book` | Prints every return and the drift line |
| `taxdiff run <files>` | Runs change set files in order, then an optional written instruction (`--text`, id CS-100, read by the parser named in `--intake`: pattern, anthropic or ollama); a refused instruction is audited and the files still run. Without `--approve` each change is a preview, shown as if the changes before it were approved; with it, each change that passes its checks is applied and `--approver` (default: the operating-system user name) is recorded. `--materiality` sets the threshold ($50 by default). `--notes` (anthropic or ollama) rewords internal reviewer notes when changes are approved, since notes are written after approval; an unavailable notes provider leaves the templates. Prints each run's lanes and reached returns, then the audit log; `--json` prints the same as JSON. Exits 0 when every change was previewed or applied, 1 when any failed validation, was stopped or was not recognized, and 2 on an error or when the `--intake` provider is unavailable |
| `taxdiff parity --refresh` | Runs Engine B live, whatever `--parity` says: the book, then the example change sets in order as previews, then each rule change alone; writes the results to the cache and applies nothing |

The options `--rules` (the starting rule pack), `--book` and `--parity` (cache, live or off) apply to
every command. `python -m taxdiff` runs the same entry point.

## Risks and mitigations

| Risk | Mitigation | What remains |
|---|---|---|
| The recorded reads miss an input, so a moved return is not reached | Engine code reads every input through the tracker; the book is recomputed for every change and any return outside the reached list that moved stops the change; tests change each of the 29 rule parameters and make 60 random fact edits | The check covers this book and these changes |
| An optional lane fails during a run | Engine B and Law run guarded and fall back as the lane table states; any other error stops the change with an audit event | None known |
| The as-filed baseline differs from what was filed | Filed returns store their filed amounts; a book without them does not load; drift is checked at session start | The shipped amounts are synthetic and were computed by Engine A under rule pack v1 |
| Engine B's verdict depends on the cache and the mode | One PolicyEngine-US release per cache, pinned in the `parity` extra; the release and mode are in the lane summary and the audit event; Route reports how many reached returns Engine B checked | In cache mode, a rule edit to a mapped parameter that was not run live first leaves the reached returns not checked |
| Engine B uses Engine A's premium derivation | Tests pin the premium rules | A mistake in the premium rules would pass both engines |
| The approver approves something other than what they saw | Approval applies the stored preview; the screen offers it only for a preview that matches the change and settings on the screen | The command line previews and applies in one command when `--approve` is given |
| An LLM misreads guidance, or text planted in the guidance steers it | Every proposed number, client and citation must appear in the pasted text, or the proposal is refused and audited; the proposal is shown, validated and previewed; a person approves | A wrong parameter key with a value from the text, or a wrong non-numeric value, depends on the approver; the preview's reached list helps |
| An LLM rewording changes a note's meaning | Figures, signs, client id and route phrase must survive; any provider failure keeps the template; client document requests are never reworded; off by default | A changed direction word next to the same signed figure |
| Approval is a typed name | Recorded with every applied change; the screen requires a name and a matching preview | No authentication; a firm deployment ties approval to user accounts |
| The audit log ends with the session | Shown on the screen, printed by the command line, included in `--json` output | No durable store; a firm deployment writes events to an append-only store |
| A shared host exposes paid or slow features | The host installs `requirements.txt` only: no LLM package, no PolicyEngine-US, no Docling. The screen offers only what is installed, so the paid option needs both a key and the `anthropic` package | An operator who installs the extras and sets a key on a public host offers the paid option to every visitor |
| Paid LLM spend on a local run | Intake makes one call; Notes makes one call per reached return that is not a document request, four at a time; both run only when chosen | No budget cap; the size of the book bounds the spend |
| Scale | Engine A recomputes the whole book for every change: 1.9 ms for 40 returns, about 0.05 ms per return | Untested above 40 returns. At scale the question is whether the whole-book check can be replaced without losing G1 |

## Alternatives considered

### Alternative A: rerun every return after each change and diff, without recorded reads

| | |
|---|---|
| Pros | Simpler: no tracker. The diff is the same, because this design also recomputes the whole book |
| Cons | It cannot show which rules and facts a changed line read. It can say a return did not move under this value, but not that the return does not depend on the changed rule, and it has no way to tell a return that moved for a reason from one that moved because of a bug |
| Decision | Rejected. The recorded reads add the reached list, the reads behind every changed line, and the check that nothing else moved. The reached list holds for any value of the edited parameter: a hypothetical cap such as CS-003 shows every return a cap could move |

### Alternative B: declare each line's dependencies in a table

| | |
|---|---|
| Pros | No tracking while computing; the table can be reviewed on its own |
| Cons | The table lists every input a line could read, not what it read for a given return, so an edit to the overtime cap would reach all 40 returns instead of the 15 with overtime. The table must be kept in step with the code by hand |
| Decision | Rejected: reads recorded per return give the narrow reached list that makes the triage useful, and the recompute checks them |

### Alternative C: ask an LLM which returns a change reaches

| | |
|---|---|
| Pros | Reads unstructured guidance directly |
| Cons | Gives no dollar change, cannot show that a return is unaffected, and a missed return goes unnoticed |
| Decision | Rejected: reviewers need amounts and a reason for each return. An LLM may propose a change set, which must match its source text and then passes validation, preview and approval, and may reword internal notes under the note check |

### Alternative D: use PolicyEngine-US as the only engine

| | |
|---|---|
| Pros | A maintained open-source model of the US tax code. It derives from OpenFisca, which has a computation tracer |
| Cons | A batch with a reform takes about 26 seconds, too slow for an interactive preview. It computes tax by rate schedule, not by the Tax Table. It takes the overtime premium as an input, so the premium would still be derived outside it. Whether its tracer can produce a reached list per change is untested |
| Decision | Rejected as the engine of record. It runs as Engine B, where a second implementation of the arithmetic catches errors in Engine A and the reverse; a single engine would leave nothing to compare against |

### Alternative E: a change log and a checklist

| | |
|---|---|
| Pros | No software to build or run |
| Cons | Review work grows with the book, and nothing shows what a reviewer missed |
| Decision | Rejected: a checklist records the change, but not the returns it reaches or how much each one moves |

### Alternative F: the core pipeline without the optional lanes

| | |
|---|---|
| Pros | Fewer parts: G1 to G6 need only Validate, Engine A, Route, Approve and Notes |
| Cons | The screen would not show how document reading, regulation text, an independent engine and an LLM fit the pipeline, which this prototype sets out to show (G7) |
| Decision | Rejected for this prototype. Each optional lane can be off or fails to a stated fallback, so the core does not depend on them |

## Non-functional requirements

| Area | Requirement |
|---|---|
| Privacy | Only synthetic data. With the Anthropic option on, instruction text and reviewer note text with synthetic amounts leave the machine; the Ollama option keeps them local. No client file goes to an LLM, and client document requests are never reworded |
| Secrets | The Anthropic key comes from `ANTHROPIC_API_KEY`, or from a local file `api key.env` at the repository root that git ignores and that holds exactly one key. It is never printed or logged |
| Performance | Measured on an AMD Ryzen 5 5600H laptop with 16 GB of memory, Windows 11 and Python 3.12, as the median of 7 runs of CS-001: Validate 0.3 ms; Engine A 2.2 ms, of which 1.9 ms recomputes the book; Engine B from the cache 1.1 ms; Route 0.4 ms. PolicyEngine-US: a batch of 38 to 40 returns 0.6 to 2.8 s after import, a batch with a reform 25.5 to 26.7 s, and a full cache refresh, import included, 2 minutes 4 seconds. Docling: the first conversion in a process 26 to 52 s including model loading, a later one 2 to 5 s, a cached read 0.001 s |
| Persistence and rollback | State and the audit log live for one browser session or one command run. Every applied change makes a new rule pack or facts version; Reset returns the session to rule pack v1 and the original facts and keeps the audit log |

## Additional resources

| Source | Link |
|---|---|
| IRS Notice 2025-69 (2025-11-21) | https://www.irs.gov/pub/irs-drop/n-25-69.pdf |
| IRS Notice 2025-62, employer reporting relief for 2025 | https://www.irs.gov/pub/irs-drop/n-25-62.pdf |
| IRS FS-2026-13, overtime FAQs (August 2026; replaced FS-2026-01 of 2026-01-23) | https://www.irs.gov/pub/taxpros/fs-2026-13.pdf |
| Schedule 1-A (Form 1040), 2025 edition | https://www.irs.gov/pub/irs-pdf/f1040s1a.pdf |
| Form 1040 instructions, 2025 edition | https://www.irs.gov/instructions/i1040gi |
| Rev. Proc. 2024-40, 2025 rate schedules and inflation adjustments | https://www.irs.gov/pub/irs-drop/rp-24-40.pdf |
| Associated Press via WTOP, filers using the new deductions before Tax Day (2026-04-15) | https://wtop.com/national/2026/04/its-tax-day-ahead-of-the-deadline-53-million-filers-used-new-trump-tax-exemptions-treasury-says/ |
| Bangor Daily News, overtime deduction claims (2026-04-17) | https://www.bangordailynews.com/2026/04/17/politics/new-overtime-tax-break-wrong-filing/ |
| FOX 13, IRS publishes Schedule 1-A instructions (2026-03-04) | https://www.fox13news.com/news/no-tax-tips-overtime-irs-publishes-instructions-claiming-deductions |
| Forvis Mazars, Arizona HB 4168 and IRC conformity (July 2026) | https://www.forvismazars.us/forsights/2026/07/arizona-updates-irc-conformity-date-to-january-1-2026 |
| PolicyEngine-US | https://github.com/PolicyEngine/policyengine-us |
| eCFR, 29 CFR part 778 | https://www.ecfr.gov/current/title-29/subtitle-B/chapter-V/subchapter-B/part-778 |

The IRS form and instruction links serve the current edition, which on 2026-11-02 is the 2025 edition.
When the 2026 editions appear, the IRS moves earlier editions under https://www.irs.gov/pub/irs-prior/.

## Future improvements

| Improvement | Purpose |
|---|---|
| Effective-dated rule pack versions | Tell a return filed under the old law from one prepared under the new law (Q3) |
| State packs | Arizona HB 4168 tips and overtime subtractions |
| 2026 forms | W-2 box 12 codes TT and TP as documents, with their own Engine B adapter entry |
| Engine release check | Treat a new definition version as a change that touches `code:` reads, so its reach shows before release; drift already shows its effect on filed returns |
| More change types | A final K-1 arriving; apportionment under IRC section 280E for dual-license dispensaries |
| Durable audit and approval | An append-only audit store, and approval tied to user accounts |
| Scale | Find whether the whole-book check can be replaced on a book of 100,000 returns without losing G1, measuring time and the memory the recorded reads take |

--------------------------------------------------------------------------------

## Detailed implementation plan

Everything below is built as of 2026-11-02, so the plan is also the order in which to rebuild or re-check
it. The repository's first commit holds the README, `.gitignore` and `.gitattributes`; the README and
`.gitignore` are modified, every other file named below is new, and nothing is deleted.

### Phases

| Phase | Builds | Checkpoint |
|---|---|---|
| P1 Packaging, rules and Engine A | `pyproject.toml`, `__init__.py`, `rules.py`, `rules/ty2025.yaml`, `provenance.py`, `provisions.py`, `tax.py`, `facts.py`, `engine.py` | `pytest tests/test_engine.py` passes |
| P2 Pipeline | `changes.py`, `impact.py`, `router.py`, `paths.py`, every module in `lanes/`, `workflow.py`, `cli.py`, `__main__.py`, the change sets, and the book from `scripts/make_book.py` | `tests/test_workflow.py`, `tests/test_lanes.py`, `tests/test_edge_cases.py` and `tests/test_safeguards.py` pass, except the tests that read the caches built in P3 |
| P3 Shipped data | The other steps in Regenerating the shipped data | The whole suite except `tests/test_app.py` passes |
| P4 Screen and documentation | `app/streamlit_app.py`, `app/style.css`, `.streamlit/config.toml`, `README.md`, `requirements.txt` | The Verification plan passes |

### Files

| File | What it holds |
|---|---|
| `src/taxdiff/rules.py` | `SCHEMA` (29 keys and their kinds), `RulePack`, `build_pack`, `load_pack`, and `edit_pack`, which makes version n+1, checks every value by its kind and requires a citation of at least three characters |
| `rules/ty2025.yaml` | Rule pack v1: every parameter with its value and citation, and a URL where the source is a published document; `overtime.definition: box14_total` |
| `src/taxdiff/provenance.py` | `Tracker` (`line`, `fact`, `rule`, `note`, `use`, `set`), `Unknown`, `total`, and the rule that engine code reads every input through the tracker |
| `src/taxdiff/provisions.py` | `OVERTIME`: `box14_total` and `flsa_premium`, each version 1 with a summary; `cents` |
| `src/taxdiff/tax.py` | `bracket_tax`, `tax_table_row`, `form_tax` |
| `src/taxdiff/facts.py` | `Person`; `ClientReturn` with `as_filed`; `Book`, which refuses a filed return without `as_filed`; `load_book`; `parse_path`; `normal_path`; `apply_fact_edit`, which refuses `client_id`, `facts_version`, `status` and `as_filed`; `validation_messages` |
| `src/taxdiff/engine.py` | `LINES`, `ComputedReturn`, and `compute`, which reads the filing status through the tracker on every line that uses it |
| `src/taxdiff/changes.py` | `RuleEdit`, `FactEdit`, `ChangeSet`, `load_change`, and `validate_change`, which returns the next rule pack and book or every error, makes one facts version per client per change set, and writes touched fact paths with `normal_path` |
| `src/taxdiff/impact.py` | `compute_book`; `find_reached`, where a return is reached when one of its reads equals a touched path or starts with that path followed by "." or "["; `analyze`, which recomputes the book once, diffs each reached return against its result before the change, gives a filed one its change in tax against `as_filed`, and lists any other return that moved as unexpected; `route_all` |
| `src/taxdiff/router.py` | `ROUTES` labels and `route(status, changed, unknown, filed_delta, materiality, engines_differ=False)`, the routing table |
| `src/taxdiff/paths.py` | Shipped file locations and `write_text_atomic`, which writes a temporary file named with the process and thread ids in the target's folder and renames it over the target; when two writers race, the last rename wins |
| `src/taxdiff/lanes/__init__.py` | Marks the lanes package |
| `src/taxdiff/lanes/llm.py` | `ANTHROPIC_MODEL`, `OLLAMA_MODEL`, `ollama_host`, `OLLAMA_HOST`, `KEY_FILE`, `anthropic_key`, `ollama_running`, `available`, `ask`, `Unavailable` (LLM contract) |
| `src/taxdiff/lanes/intake.py` | `parse`, `PROMPT`, `numbers`, `unsupported` and `propose` (Matching rules) |
| `src/taxdiff/lanes/documents.py` | `FIELDS`, `Statement`, `read_text`, `read_statement` |
| `src/taxdiff/lanes/engine_b.py` | `COMPARED`, `FILING`, `PARAMETERS`, `return_inputs`, `baseline_pack`, `reform_for`, `ADAPTER_VERSION`, `cache_key`, `load_cache`, `save_cache`, `run_policyengine`, `installed_version`, `run_lane`, `unavailable`, `compare`, `parity_for` (Engine B adapter) |
| `src/taxdiff/lanes/law.py` | `STATIC` citations, `FLSA`, `BY_PREFIX`, `BY_LINE`, `authorities` (cached for the life of the process, so a running server sees a rebuilt `ecfr.json` only after a restart), `for_rules`, `for_lines` |
| `src/taxdiff/lanes/notes.py` | `reviewer_note`, `document_request`, `figures`, `POLISH_PROMPT`, `polish` |
| `src/taxdiff/workflow.py` | `LaneResult`, `AuditEvent`, `State`, `Run`, `pack_digest`, `filed_amounts`, `initial_state`, `run_change`, `approve_run`, `record_intake_refusal`, `impact_rows` |
| `src/taxdiff/cli.py`, `__main__.py`, `__init__.py` | The command line, run by the `taxdiff` script or `python -m taxdiff`; `__version__ = "0.1.0"` |
| `app/streamlit_app.py`, `app/style.css`, `.streamlit/config.toml` | The review screen (Screen), its styles, and the light theme |
| `changes/` | CS-001 sets `overtime.definition` to `flsa_premium`; CS-002 is C-017's statement as text; CS-003 sets a hypothetical overtime cap of $2,500; CS-004 is CS-002's statement as a PDF; CS-900 is a negative cap without a citation; CS-901 is a statement for tax year 2024 |
| `data/book_ty2025.json`, `data/coverage.json` | The synthetic book of 40 returns and the coverage case of each client |
| `data/documents/` | Two synthetic employer statements for C-017, for tax years 2025 and 2024, as text and as PDF |
| `cache/documents/<sha256>.md` | Docling's Markdown for each PDF, named by the PDF's SHA-256 |
| `cache/law/raw/29-cfr-778.107.xml`, `cache/law/raw/29-cfr-778.110.xml`, `cache/law/ecfr.json` | Saved eCFR section XML, and the excerpts built from it |
| `cache/policyengine.json` | PolicyEngine-US results (Data formats) |
| `scripts/make_book.py`, `scripts/make_documents.py`, `scripts/refresh_law.py` | Build the shipped data (Regenerating the shipped data) |
| `tests/` | `__init__.py`, `conftest.py` and six test files (Tests) |
| `README.md`, `pyproject.toml`, `requirements.txt`, `.gitignore` | Setup, examples and expected outputs; packaging (Dependencies); the ignored key file |

### Interfaces

| Function | Contract |
|---|---|
| `workflow.initial_state(pack, book, parity_mode="cache") -> State` | Computes every return, runs Engine B (a lane that raises leaves every return not checked), reads the as-filed amounts and lists drift |
| `workflow.run_change(state, change, *, approve=False, materiality=Decimal(50), parity_mode="cache", intake=None, notes_provider=None, docling_live=False, on_lane=None, approver="reviewer") -> Run` | Runs a preview. `intake` is the intake lane's result for a written instruction; `docling_live=True` converts a PDF again; `on_lane(LaneResult)` is called on the caller's thread as each lane finishes. With `approve=True` the preview goes to `approve_run`. Never raises: an error ends in an error event |
| `workflow.approve_run(preview, approver, *, notes_provider=None, on_lane=None) -> Run` | Applies `preview.pending` and writes the notes, rewording up to four reviewer notes at a time, and at most `NOTES_REWORD_MAX` (8) per approval, when `notes_provider` is set. Raises `ValueError` for a run that is not a passing preview; a blank approver gives a refused event |
| `workflow.record_intake_refusal(state, change_id, text, source, reason) -> State` | Adds a "not recognized" event for an instruction the intake lane could not read or refused |
| `intake.propose(text, change_id, provider="pattern") -> ChangeSet` | Raises `ValueError` for an unreadable, empty or mixed proposal or one `unsupported` faults, and `llm.Unavailable` when the provider cannot run |
| `llm.ask(provider, prompt, *, json_output=False, max_tokens=600) -> str` | Raises `llm.Unavailable` for every failure: no key, no package, an API error, no server, an unreadable reply, an unknown provider |
| `llm.available() -> dict[str, bool]` | `anthropic` when a key and the `anthropic` package are both present; `ollama` when `/api/tags` answers within 1 s |
| `documents.read_text(path, use_cache=True) -> tuple[str, str]` | The text and the method: "pattern map" for a text file; for a PDF, "Docling (cached conversion), then pattern map" or "Docling, then pattern map". With `use_cache=False` and Docling not installed it raises `ValueError` |
| `documents.read_statement(text, method, client_id, book) -> Statement` | The fields read, field errors, and fact edits on the person whose SSN digits match |
| `engine_b.run_lane(book, pack, mode="cache") -> LaneOutput` | "cache" reads results; "live" recomputes every checkable return and writes the results, or checks nothing when PolicyEngine-US is not installed; "off" checks nothing |
| `engine_b.parity_for(results, lane, unmapped_reads) -> dict[str, Parity]` | Agree, differs or not checked per return; `unmapped_reads` names the returns that read a changed parameter outside the mapped set |
| `law.for_rules(keys)`, `law.for_lines(lines) -> list[Authority]` | Authorities by key prefix and by line, without duplicates |
| `notes.polish(note, client_id, provider, *, route="") -> tuple[str, str]` | The text and its source; any provider failure keeps the template; an empty `route` skips the route check |

### Data formats

| Data | Format |
|---|---|
| Parity cache key | `sha256(json.dumps({"tax_year": 2025, "adapter": ADAPTER_VERSION, "inputs": inputs, "reform": reform}, sort_keys=True).encode()).hexdigest()[:24]` with the default separators. `ADAPTER_VERSION` is 3 and goes up whenever `return_inputs` or the compared lines change |
| `cache/policyengine.json` | `{"policyengine_us": "2.90.4", "results": {<key>: {<output variable>: <float>, "policyengine_us": "2.90.4"}}, "runs": [{"at", "returns", "seconds", "policyengine_us", "adapter", "reform"}]}`, written with `indent=1` and sorted keys |
| `cache/law/ecfr.json` | `{"source": "eCFR versioner API", "as_of": "2026-10-29", "built": <date>, "sections": {"29 CFR 778.107": {"heading", "excerpt", "url"}, "29 CFR 778.110": {...}}}`. The heading is the section XML's first `HEAD` element and the excerpt its first `P` element, with tags removed and spaces collapsed |
| `cache/documents/<sha256>.md` | Docling's `export_to_markdown()` output for the PDF with that SHA-256, converted without OCR and without table structure |
| `as_filed` | Per filed return, `{line: amount}` for every line in `LINES` except `tax_formula`, with amounts as decimal strings in dollars, for example `{"tax": "7955"}` |
| Audit pack hash | The first 12 hex digits of the SHA-256 of `json.dumps(pack.raw without "version", sort_keys=True, default=str)` |
| `run --json` output | `{"runs": [{"change", "applied", "previewed", "errors", "lanes": [{"lane", "state", "ms", "summary"}], "reached": [{"client", "status", "route", "lines", "tax_change", "vs_filed", "needs"}], "unexpected", "tax_after"}], "audit": [<every AuditEvent field>]}` |

### Engine B adapter

| Engine A | PolicyEngine-US 2.90.4 |
|---|---|
| Wages | `employment_income` |
| Birth date | `age`: 2025 minus the birth year, at least 65 when born before `age65.born_before` and otherwise at least 18 |
| Premium from the rule pack's definition | `fsla_overtime_premium`, in PolicyEngine-US's spelling |
| Qualified tips and whether the occupation is listed | `tip_income`, `tip_income_deduction_occupation_requirement_met` |
| Filing status single, mfj, mfs, hoh | `SINGLE`, `JOINT`, `SEPARATE`, `HEAD_OF_HOUSEHOLD` |
| `overtime.cap.other`, `overtime.cap.mfj` | `gov.irs.deductions.overtime_income.cap.SINGLE` and `.HEAD_OF_HOUSEHOLD`; `.JOINT` |
| `overtime.phaseout.start.other`, `overtime.phaseout.start.mfj` | `gov.irs.deductions.overtime_income.phase_out.start.SINGLE` and `.HEAD_OF_HOUSEHOLD`; `.JOINT` |
| `tips.cap`, `tips.phaseout.start.other`, `tips.phaseout.start.mfj` | `gov.irs.deductions.tip_income.cap`; `gov.irs.deductions.tip_income.phase_out.start.SINGLE` and `.HEAD_OF_HOUSEHOLD`; `.JOINT` |
| `senior.amount` | `gov.irs.deductions.senior_deduction.amount` |
| Compared lines | `standard_deduction`, `tip_income_deduction`, `overtime_income_deduction`, `additional_senior_deduction`, `taxable_income`, and `income_tax_before_credits` against Engine A's rate-schedule tax |

One simulation holds every checkable return. Each return is its own tax unit, family, SPM unit and
household (state TX), with one marital unit for a joint return and one per person otherwise. A reform
overrides the mapped parameters from 2025-01-01. A line agrees when `abs(engine_a - round(policyengine, 2))
<= 0.50`, and a return differs when any compared line does not agree.

### LLM contract

| Item | Detail |
|---|---|
| Anthropic | The `anthropic` package; model `TAXDIFF_ANTHROPIC_MODEL` (default `claude-haiku-5-5`); timeout 60 s; one retry |
| Ollama | The standard library's `urllib`: `POST <OLLAMA_HOST>/api/chat` with `stream: false`, `think: false` and, for intake, `format: "json"`; model `TAXDIFF_OLLAMA_MODEL` (default `qwen3.5:2b`); timeout 180 s |
| `OLLAMA_HOST` | Read once at import. A missing scheme becomes http, a missing port 11434, and `0.0.0.0` (a server bind address) localhost |
| Key | `ANTHROPIC_API_KEY` first; otherwise `api key.env` at the repository root, which must hold exactly one value shaped `sk-ant-...`, alone or after `=` or `:` |
| Intake request | `intake.PROMPT` lists the rule keys and asks for `{"rule_edits": [{"path", "to", "cite"}], "fact_edits": [{"client_id", "path", "to"}]}`, for example `{"rule_edits": [{"path": "overtime.cap.other", "to": 12500, "cite": "Notice 2025-69"}], "fact_edits": []}`; up to 600 tokens |
| Intake reply | Read from its first `{` to its last `}`, which drops prose and code fences; anything that does not parse into edits is unreadable |
| Notes | `notes.POLISH_PROMPT`, up to 300 tokens per note, four notes at a time |
| Errors | Callers see only `ValueError` from intake and `llm.Unavailable`; a note keeps its template on any failure |

### Matching rules

| Check | Rule | Examples |
|---|---|---|
| Numbers in intake text (`intake.numbers`) | Every run of digits with optional commas and decimals; commas removed, leading zeros and trailing zero decimals dropped, signs ignored | "$12,500" and "12500.00" both read 12500; "05" reads 5 |
| A proposed number (`unsupported`) | Found when the number, or for a number under 1 the number times 100, is among the text's numbers | 0.05 matches "5%"; 15000 does not match a text whose only figure is 12,500 |
| A proposed citation | Found when it appears in the text, ignoring case and spacing, or when every number in it is among the text's numbers; a citation with no number must appear | "IRS Notice 2025-69" matches a text with "Notice 2025-69"; "Notice 2026-12" does not |
| A proposed client | Its id appears in the text | |
| Pattern parser (`intake.parse`) | One instruction per line: `set [client id] <path> to <value> [per, citing or cite <citation>]`. A value is true, false, null, none or empty, a number with optional `$` and commas, or text that validation reads by the parameter's kind. A text that mixes rule and fact lines is refused | `set C-019 people[0].ot_flsa_hours to 150` |
| Note figures (`notes.figures`) | The regex `([+-]?)\$?(\d[\d,]*(?:\.\d+)?)`; the sign is kept, commas removed and trailing zero cents dropped. The hyphen in an id reads as a sign, the same way in the template and the rewording | "+$1,920.00" reads +1920; "C-017" reads -017 |
| Route phrase | The route label must appear in the rewording, ignoring case | "amendment review" |

### Authorities and note templates

| Rule key prefix | Authorities (`law.BY_PREFIX`) |
|---|---|
| `overtime.` | IRC 225; Notice 2025-69; Schedule 1-A; 29 CFR 778.107; 29 CFR 778.110 |
| `tips.` | IRC 224; Notice 2025-69; Schedule 1-A |
| `senior.` | IRC 151(d)(5)(C); Schedule 1-A |
| `standard_deduction.` | 2025 Form 1040 instructions; Rev. Proc. 2024-40 |
| `age65.` | Publication 501 |
| `brackets.` | Rev. Proc. 2024-40 |
| `tax_table.` | 2025 Form 1040 instructions |

`law.BY_LINE` maps the overtime, tips, senior and standard deduction lines to the same lists, the tax line
to the Form 1040 instructions and Rev. Proc. 2024-40, and the rate-schedule tax line to Rev. Proc. 2024-40.
`law.STATIC` holds each authority's citation text and URL.

A reviewer note reads "<client> (<status>): <route in lower case>. Cause: <change id> <title>.", then
"<line label>: <before> to <after>." for each changed line other than modified AGI and the rate-schedule
tax, then "Tax change: <signed amount>." and, for a filed return, "Against the return as filed: <signed
amount>." A document request reads "Request for <client>: please send <needs> for tax year <the book's
tax year>. We need it to finish the Schedule 1-A overtime deduction on your return."

### Screen

| Control | Widget | Default | Behavior |
|---|---|---|---|
| Source | Radio: example change set or written instruction | Example change set | |
| Change set | Select box over `changes/*.yaml` | The first file | Shows the YAML |
| Read the text with | Radio | Pattern parser | LLM options appear only when `llm.available()` reports them |
| Instruction and "Read instruction" | Text area and button | Empty | The proposal is stored for the session and shown as JSON; a refusal is shown and audited |
| Approver | Text input | Empty, with the placeholder "Your name" | Required to approve |
| Materiality for filed returns ($) | Number input, step 10 | 50 | |
| PolicyEngine-US parity | Radio: cached results, run live, off | Cached results | "Run live" appears only with PolicyEngine-US installed |
| Convert PDF statements again with Docling | Checkbox | Off | Disabled without Docling |
| Reviewer note wording | Select box | Templates | LLM options appear only when available |
| "Preview", "Approve and apply", "Reset book" | Buttons | | "Approve and apply" is enabled only when the latest run is a preview of the change set on the screen with the same materiality, Engine B mode and Docling setting, and the approver is filled in |

The session keeps `state` (the `State`), `runs` (every `Run`), `written` (the next written instruction's
number, from 100), `proposal` (the stored intake proposal) and `previewed` (the settings of the latest
preview). The lane board has eight stages (Engine A alone, then Engine B and Law together) and fills each
lane's bar in 220 ms with a 60 ms step per stage; under `prefers-reduced-motion` it shows the final state. Colors are CSS custom properties in `app/style.css`:
neutrals `#040404`, `#1d1d1d`, `#3d3d3d`, `#676767`, `#9a9a9a`, `#c0c0c0`, `#e4e4e4`, `#f4f4f4` and
`#ffffff`; routes update `#0b5cad`, reviewer `#9a6700`, amendment `#b42318`, document request `#6f42c1`,
and `#676767` for the other two; lane states done `#1a7f37`, failed `#b42318`, waiting `#9a6700`, skipped
`#9a9a9a`; changed lines are marked with the amendment color; the primary button and focus ring are
`#1f4e79`. `.streamlit/config.toml` sets the theme from the same values. The table styler cannot read
custom properties, so the app repeats the route and parity colors as hex values.

### Regenerating the shipped data

Run these in order; each step uses the one before it.

1. `python scripts/make_book.py --seed 7` writes the book and the coverage list. C-017's SSN digits (8313)
   are written out because the statements carry them. The as-filed amounts are a record: regenerating the
   book after an engine change would rewrite them and hide drift, so regenerate only to make a new book.
2. `python scripts/make_documents.py` renders each statement as a PDF with a fixed creation date, so the
   bytes and the SHA-256 stay the same between runs. Then, with the `docs` extra, call
   `documents.read_text(pdf, use_cache=False)` for each PDF to write its conversion, and delete any
   conversion whose SHA-256 matches no PDF.
3. With the `parity` extra, `python -m taxdiff parity --refresh` runs PolicyEngine-US live: the book
   under rule pack v1; CS-001 to CS-004 in order, each from the state the one before it would apply; then
   each rule change alone on the book as shipped. On 2026-11-02 this took 2 minutes 4 seconds.

Independently of these, the eCFR excerpts are rebuilt by saving the XML of 29 CFR 778.107 and 778.110 from
`https://www.ecfr.gov/api/versioner/v1/full/<date>/title-29.xml?part=778&section=<section>` as
`cache/law/raw/29-cfr-<section>.xml`, then running `python scripts/refresh_law.py --as-of <date>` with the
same date.

### Tests

`tests/conftest.py` provides the session fixtures `pack_v1` and `pack_v2` (v2 sets `overtime.definition` to
`flsa_premium`) and the helpers `person()` and `make_return()`, which builds client C-999. Tests stub LLM
calls at `llm.ask`, which intake and notes call through the module, and patch these module attributes:
`llm.available`, `llm.KEY_FILE`, `llm.ollama_running`, `llm.urllib.request.urlopen`, `llm.importlib.util.find_spec`; `engine_b.load_cache`,
`installed_version`, `save_cache`, `run_policyengine` and `run_lane`; `law.ECFR_CACHE` and `law.for_rules`;
`workflow.analyze` and `workflow.validate_change`; `impact.find_reached`; `engine.form_tax`. Because
`law.authorities` and `engine_b.baseline_pack` are cached, a test that changes their inputs calls
`cache_clear()`.

| File | Tests | What they cover |
|---|---|---|
| `test_engine.py` | 50 | Tax Table cells and worksheet results; Schedule 1-A boundaries (MAGI $150,999 and $151,000, birth dates 1961-01-01 and 1961-01-02, the per-spouse senior phase-out); the Notice 2025-69 premium rules, method C included (example 3: $10,000 at 2x gives $5,000); golden returns |
| `test_workflow.py` | 20 | Scenario 1's reach, route counts and tax changes (C-003 +$480, C-033 +$48); C-031 at -$660 and C-017 at +$1,151 against filed; the ranges while a document is outstanding (C-017 +$1,030 to +$1,580, C-019 +$928 to +$1,632, hand-computed from the 2025 Tax Table) and bounds only when the pay multiple is known; notes and notices carrying the range; CS-003 after CS-001; scenario 3, with every problem in a rule change reported; the change stopped when `find_reached` hides a reached return; previews by default; each of the 29 rule parameters changed (money +$1,000, rates halved, rate schedule limits +$1,000, dates a day later, the other definition) and 60 seeded random fact edits (seed 11), each with no unexpected return; the pattern parser; cached parity before and after CS-001 |
| `test_lanes.py` | 33 | eCFR excerpts; stubbed LLM proposals; the note figure check; letters kept apart from internal notes and never reworded, with interest, penalty and refund-window wording; the cached PDF conversion; the PDF and text statements giving one result; the routing table, including $50.00 to Amendment review, -$49.99 to Below materiality, Unknown before an engine disagreement, no new step for a return still waiting, and a contradicting document routed whatever the rules |
| `test_edge_cases.py` | 15 | Unknown tips eligibility; a phase-out that removes the cap; a whole-person edit; status as workflow; non-finite numbers; a flipped sign in a note; a key file with two keys; an unreadable Ollama reply; invalid SSNs in parity; a live run over 40 returns; the cache key; the full senior deduction for a birth date of 1961-01-01 |
| `test_safeguards.py` | 42 | Lane failures (unreadable parity and eCFR caches, Engine B, Law and Engine A raising, any other error, live parity without PolicyEngine-US, a new release starting the cache again); approval (applies the preview, refuses a blank name and a non-preview, skips Engine B and Law when Engine A stops); G2 timing; the audit fields; as-filed amounts and drift (another starting rule pack, a simulated engine release, a book without `as_filed`); a padded path index; command-line exit codes and an end-to-end `--json` run; refused instructions audited, with the files still run and JSON still printed; intake matching; a malformed LLM reply; a provider crash in notes; the route phrase; `available()` and `ollama_host` |
| `test_app.py` | 5 | The screen: preview then approve CS-001 and CS-004; a preview applies nothing and shows as-filed values; approval needs a name and a matching preview; Reset keeps the audit log; a written instruction must be read, previewed and approved |
| `test_api.py` | 70 | The web API: every change set gives the command line's result; a sequence of approvals; dashboard figures equal to the CLI's; rejections with field reasons; node order and measured durations; no key and eCFR unreachable; the Explain figure check; forged preview tokens, missing receipts, injected and reordered histories; JSON-only same-site requests; size, throttle and model limits; the key never in a response; audited refusals and errors; answers written by code with a model on; a model change that fails validation giving way to the code; a citation that contradicts the pack, and an authority not found at its address, stopping a change; a statement contradicting a filed return flagged before the rules are corrected; unique change ids and a statement's saved id |
| `test_questions.py` | 63 | Reviewer phrasings read by code: numbers bound to their units, facts stated in words, the guard that no reading leaves out a stated fact, yes-or-no and why-questions about a client, changes, relative edits in every common form, several facts in one sentence, facts stated in plain English, questions about a client and about the session, items not modeled, and what is refused |
| `test_intake_eval.py` | 1 | Both eval sets replayed from the recorded runs through every check: no wrong reading accepted in any run, and the published score current |
| `e2e/test_dashboard.py` | 32 | The dashboard in a browser (skipped unless `TAXDIFF_E2E_URL` is set): every example to the end, the approval gate, a node drawer, a return's chain, CS-900, 375 px, reduced motion, an unsupported question; any console error fails a test |

### Dependencies

Python 3.12. Base: `pydantic>=2.7`, `pyyaml>=6`, `fastapi>=0.115` and `anthropic>=2.3,<3`, which is what the
hosted API installs. Extras: `ui` is `streamlit>=1.66` (the release the screen is tested on) and
`pandas>=2.1` (for the table styler); `parity` is
`policyengine-us==2.90.4`, the release the cache and the tolerance were measured on, since another release
starts the cache again on its first live run; `llm` is `anthropic>=0.40`; `docs` is `docling>=2.40` and
`fpdf2>=2.8`; `dev` is `pytest>=8`, `ruff>=0.6`, `httpx`, `uvicorn` and `playwright`. Ollama is reached with the standard library. Ruff
checks the rule groups E, F, W, I, B and UP, ignores E501 and UP007, and uses a line length of 120. Pytest
reads `tests/` with `src/` on the path. `uv.lock` locks every extra, and `requirements.txt` is the
base set exported from it with exact versions (the ones the tests ran on), for the host, and `.gitignore` ignores `*.env`, which covers `api key.env`. The dashboard needs Node 20.9
or later; its packages are pinned in `web/package.json`.

### Verification plan

| Check | Command or method | Passes when |
|---|---|---|
| Tests | In a fresh virtual environment, `pip install -e ".[dev,ui]"`, then `pytest`, with `ANTHROPIC_API_KEY` unset | Exit code 0 and 299 passed; the 32 browser tests skip unless `TAXDIFF_E2E_URL` is set. The suite makes no network call: every provider and public API is stubbed, and the model's replies in the intake eval are replayed |
| Lint | `ruff check src tests app scripts` | Exit code 0 |
| End to end, applied | `taxdiff --parity cache run changes/001-overtime-premium.yaml changes/002-employer-statement-c017.yaml --approve --approver test --json` | Exit code 0; CS-001 reaches 15 returns with the routes in scenario 1 and 0 unexpected; CS-002 reaches C-017 alone, routed to Amendment review at +$1,151 against filed; the audit reads previewed, applied, previewed, applied. `test_cli_end_to_end_matches_the_readme` asserts this |
| End to end, preview | The same command without `--approve` and `--json` | The same routes and amounts, with "Preview only" printed |
| Rejections | `taxdiff run changes/900-invalid-negative-cap.yaml`, and the same for `changes/901-statement-wrong-year.yaml` | Exit code 1, with one reason per field |
| Book | `taxdiff book` | Every return shows `agree`, and the drift line says all 14 filed returns recompute to their filed amounts |
| Live paths, by hand | `taxdiff --parity live run changes/001-overtime-premium.yaml` with the `parity` extra; CS-004 on the screen with "Convert PDF statements again" and the `docs` extra; `taxdiff run --text "<guidance>" --intake ollama` with Ollama running, and `--intake anthropic` with a key | PolicyEngine-US agrees on 38 returns and leaves 2 not checked; the conversion matches the cached one; a proposal whose numbers, client and citation are in the text passes, and one that is not is refused and audited |
| Design-to-code check | Compare this document with the code | No difference remains |
