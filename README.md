# TaxDiff

Change-impact triage for prepared tax returns.

When a tax rule or a client's documents change after returns are prepared, TaxDiff finds every return the
change reaches, shows how much each line moves, and routes each return to its next step: update, back to
the reviewer, amendment review, or a document request. Every number comes from deterministic code with its
source recorded, and nothing is applied until a named reviewer approves the exact preview.

- **Live app:** https://taxdiff-app.vercel.app (synthetic data only; `/status` checks every endpoint and outside service)
- **Design document:** [docs/designs/taxdiff.md](docs/designs/taxdiff.md)

## The main example

Release 1 of the engine counted all W-2 box 14 overtime pay as qualified overtime. IRS Notice 2025-69 counts
only the premium the Fair Labor Standards Act requires: the extra half in time-and-a-half. Correcting the rule
(change set CS-001) reaches 15 of the 40 returns in the sample book:

| Route | Returns |
|---|---|
| Amendment review, with a client notice | 5 filed returns (C-017 also waits for an employer statement; until it arrives its tax change is bounded at +$1,030 to +$1,580) |
| Back to reviewer | 4 returns in review |
| Update return | 3 returns in preparation |
| Document request | 1 return in review |
| Too small to amend: tell the client | 1 filed return that moves by less than the $50 threshold |
| No action | 1 return that reads the rule but does not move |

A full recompute confirms the other 25 returns did not move.

## Why it matters

Returns are computed against law and facts that keep changing after preparation: IRS guidance arrives during
the filing season, preparation software corrects its own reading of the law, corrected statements arrive
after a return is filed, and clients remember details late. Batch recompute can rerun returns, but it does not
say why a return moved, show that the others could not move, or send a filed return to amendment review with
a notice to the client. TaxDiff adds those from what each computed line recorded it read.

The 2025 overtime deduction is a current case. About 21 million filers claimed it before Tax Day 2026 (AP,
Apr 15 2026), and employers were not required to report the FLSA premium separately for 2025 (IRS Notice
2025-62), so a return computed from the full box 14 amount overstates the deduction.

## Quickstart

Requires Python 3.12 and Node 20.9 or later. No paid service or account is needed: without an Anthropic key,
the code reader and the note templates run on their own.

```bash
pip install -e ".[dev]"
pytest                                                   # 299 tests, no key and no network
uvicorn taxdiff.api.server:app --app-dir src --port 8000
cd web && npm install && npm run dev                     # http://localhost:3000, /api goes to port 8000
```

The same pipeline runs from the command line:

```bash
python -m taxdiff book
python -m taxdiff run changes/001-overtime-premium.yaml                  # preview: routes and amounts, nothing applied
python -m taxdiff run changes/001-overtime-premium.yaml --approve
python -m taxdiff run changes/900-invalid-negative-cap.yaml --json       # rejected, with a reason for each field
python -m taxdiff run --text "set overtime.cap.other to 2500 per IRC 225" --approve
```

A written instruction uses one line per change: `set <rule key> to <value> per <citation>` for a rule, or
`set <client id> <fact path> to <value>` for a fact (for example `set C-019 people[0].ot_flsa_hours to 150`).
`run` exits with 0 when every change was previewed or applied, 1 when any failed validation or was not
recognized, and 2 on an error.

## How it works

1. **Rule packs** hold tax parameters as versioned data, each value with its citation (`rules/ty2025.yaml`).
   Definitions that need code, such as what counts as qualified overtime, are named, versioned functions in
   `src/taxdiff/provisions.py`.
2. **Provenance.** The engine records, for every computed line, the rules, facts and definition versions it read.
3. **Validation.** A change set is checked for keys, types, ranges and citations. An invalid change is
   rejected with a reason for each field, and the last valid version stays in effect.
4. **Reach.** The engine recomputes the whole book once. Returns whose recorded reads include the change are
   reached and diffed line by line; if any other return moved, the change is stopped.
5. **Cross-check.** PolicyEngine-US, a separate open-source model of the tax code, recomputes the same returns
   as a check on the arithmetic. Disagreements are shown with their cause.
6. **Routing.** Each reached return gets its next step from the table below.
7. **Approval.** Nothing is applied until a named person approves the preview. Approval applies exactly that
   preview, writes internal notes and client letters, and adds an audit log entry.

```
intake ─► documents ─► validate ─► Engine A ─┬─► Engine B ─┬─► route ─► approve ─► notes
                                             └─► law ──────┘
```

Rows are checked in order and the first match wins:

| Condition | Next step |
|---|---|
| A new statement or corrected facts support less qualified overtime than the return deducted | Amendment review for a filed return (or "too small to amend" under the firm's threshold); back to reviewer for a return in progress |
| No line changed | No action |
| A required fact is missing | Document request; a filed return also goes to amendment review. The tax change is shown as a range, computed with the missing amount at each end |
| The two engines disagree | Back to reviewer, with the cause when it is a known one |
| Return in preparation | Update return |
| Return in review | Back to reviewer, with the line diff |
| Filed return, tax change below the threshold ($50 by default) | Too small to amend: the client is told (Circular 230 section 10.21) |
| Filed return, otherwise | Amendment review, with a client notice |

**Letters and notes.** Internal notes and client letters are kept apart. A client letter says what the change
means for the client (interest under IRC 6601, the accuracy-related penalty only when the increase could reach
a substantial understatement, or the refund time limit under IRC 6511(a)). Letters are drafted by code and
shown before approval. An internal note may be reworded by the model, but its decision sentence and every
figure must match the template, or the template is kept.

**Authorities.** A rule edit must cite an authority that can set that parameter: an amount fixed by the Code
changes only by its own Code section or a law that amends it, an inflation-adjusted amount also by revenue
procedure, and the definition of qualified overtime also by regulation or IRS guidance. Superseded authorities
are refused. On the hosted app each cited authority with a predictable address is looked up; the technical
approver confirms that it sets the value. A change cited as hypothetical is a what-if: previewed, never applied.

**Where AI is used.** Code reads a question first. The Anthropic API (`claude-haiku-5-5`) reads only what the
code cannot, such as a pasted paragraph of guidance or free-form wording, and its reading is refused unless
every number, client and citation appears in the text. It also rewords internal notes. It never computes a
number, never writes an answer or a client letter, and the app runs without it.

## The dashboard

The web app (Next.js in `web/`, FastAPI in `src/taxdiff/api/`) opens on one sentence about the problem, the
question box and the 40 sample returns. Type a question or a change, paste IRS guidance, edit a client's facts
with the form, or pick an example. While a run streams, its 12 steps light up on a workflow graph with their
measured times; then the result comes first, led by one return's story from start to finish. A return opens
to its Schedule 1-A worksheet before and after, the Notice 2025-69 method, and, for a filed return, a Form
1040-X style preview. The worklist is grouped by route with sign-off by role and CSV export.

| Step | Hosted app | Run locally |
|---|---|---|
| Read the question, word notes | Code first; the Anthropic API when the server has a key | The key from `ANTHROPIC_API_KEY`; or `TAXDIFF_LLM=ollama` |
| Validate, recompute, route, audit | This project's code on every request | The same |
| Cite the law | The eCFR API, each cited source checked at its address, and a Federal Register search; the saved copy if eCFR does not answer within 3 s | `TAXDIFF_LAW=cache` to stay offline |
| Cross-check | The public PolicyEngine API; cached PolicyEngine-US 2.90.4 results if it does not answer within 5 s | `TAXDIFF_PARITY=api`, `live` (with the `parity` extra) or `cache` |
| Read documents | Text statements live; PDFs through cached Docling conversions | `TAXDIFF_DOCLING=live` with the `docs` extra |

The server keeps no session. The browser holds the approved change sets, each with a receipt the server signed,
and every request replays them; approval needs the signed token of the preview the reviewer saw, and the
server refuses if any route or amount differs. Names are typed, not verified, and the audit log stays in the
browser tab.

## Example inputs and expected outputs

On the dashboard, starting from rule pack v1 (asserted in `tests/test_api.py`):

| Question | Expected |
|---|---|
| Run CS-001: count only the FLSA-required overtime premium | Waits for approval with 15 of 40 reached: amendment review 5, back to reviewer 4, update 3, document request 1, too small to amend 1, no action 1. More tax on 11 returns (+$8,028), less on 1 (-$660), 2 waiting on documents. After approval: rule pack v2, notes for every reached return, letters for the filed returns that moved |
| C-017's employer sent a corrected overtime statement (after CS-001) | Reaches C-017 only: amendment review, +$1,151 against the return as filed |
| Which filed returns need amending? (after CS-001) | C-017 (+$1,030 to +$1,580 until its statement arrives), C-024 +$672, C-026 +$1,440, C-029 +$648, C-031 -$660; C-033 +$48 is too small to amend |
| C-021's corrected W-2 shows wages of $149,000 | A fact change: C-021's income drops under the $150,000 phase-out start; tax -$624; back to reviewer |
| What if the overtime cap drops to $10,000? | Preview only, never applied: the cap for single and head of household filers from $12,500 to $10,000 reaches 12 of 40 returns |
| Run CS-900 (a negative cap with no citation) | Failed validation: the value must be zero or more, and a citation is required. Nothing changes |
| What is the weather in Irvine? | Not run: the answer says what the tool reads and suggests questions |

From the command line (inputs in `changes/` and `data/`; expected values asserted in `tests/test_workflow.py`,
`tests/test_lanes.py` and `tests/test_safeguards.py`):

| Input | Expected output |
|---|---|
| `001-overtime-premium.yaml` | Reaches 15 of 40; 25 not reached; 0 unexpected changes in the full recompute. C-003 tax +$480; C-031 filed, -$660 (an amended return would claim the refund); C-033 filed, +$48 (too small to amend; the client is told) |
| `002-employer-statement-c017.yaml`, after 001 | The documents lane reads 160 FLSA hours at $24.00, a $1,920.00 premium. Reaches C-017 only: amendment review, +$1,151 |
| `004-employer-statement-c017-pdf.yaml`, after 001 | The same statement as a PDF, read through the cached Docling conversion; the same result as 002 |
| `003-hypothetical-overtime-cap.yaml`, after 001 | A what-if: previewed only, and `--approve` refuses it. Reaches 12 returns |
| `900-invalid-negative-cap.yaml` | Failed validation: `overtime.cap.other: must be zero or more`; `overtime.cap.other.cite: a citation is required`. Rule pack stays at v1 |
| `901-statement-wrong-year.yaml` | Failed validation: `tax_year: statement is for 2024; the book is 2025` |

## Sample data

All data is synthetic. `scripts/make_book.py` writes the 40-return book from 25 coverage cases plus 15 seeded
filler returns: 14 in preparation, 12 in review, 14 filed. Employer statements are in `data/documents/` as
text and PDF.

| Coverage case | Clients |
|---|---|
| 1.5x FLSA overtime, box 14 total only | C-003, C-012, C-024; C-005 (spouse); C-014 (head of household) |
| Overtime above the single cap | C-026 |
| 2x pay for FLSA overtime hours | C-016 |
| Overtime required only by state law | C-029 |
| Mixed FLSA and state overtime, no split | C-019, C-017 (statement arrives in change 002) |
| FLSA hours and regular rate, no box 14 amount | C-031 |
| Employer statement already on file | C-008 |
| Income in the overtime phase-out | C-021 |
| Change below the amend threshold | C-033 |
| Married filing separately | C-010 (with overtime), C-039 |
| Tips, listed and unlisted occupation | C-001, C-022; C-035 |
| Age 65 boundary: born Jan 1 vs Jan 2 1961 | C-006, C-007 |
| Seniors in the phase-out | C-018 (both spouses), C-027 |
| One spouse 65 or older, below the phase-out | C-036 |
| Head of household, no overtime | C-011 |

## Scope

| Status | Item |
|---|---|
| Implemented | Tax year 2025 Form 1040: Tax Table and Tax Computation Worksheet, standard deduction with the age 65 amount, Schedule 1-A qualified tips, qualified overtime and the senior deduction with their phase-outs |
| Implemented | Rule pack with citations, provenance per line, change sets, validation, preview, approval, routing, notes and letters, audit log, full-recompute check, stored as-filed amounts with a drift check |
| Cached (labeled) | PolicyEngine-US results in `cache/policyengine.json`; Docling conversions in `cache/documents/`; FLSA excerpts in `cache/law/ecfr.json` |
| Synthetic | The client book and employer statements |
| Optional | Model reading and note wording (Anthropic API, paid; or a local Ollama model); Docling for new PDFs |
| Not modeled | AMT, capital-gain rates, credits, QBI, SALT, car-loan interest, state returns, effective dates within a year, e-file |

## Configuration

| Variable | Purpose |
|---|---|
| `TAXDIFF_SECRET` | Signing key for preview tokens and receipts; required on the host so every instance signs the same way |
| `ANTHROPIC_API_KEY` | Optional. Enables model reading and note wording; use a key with a monthly spend limit |
| `TAXDIFF_ANTHROPIC_MODEL` | Model name (default `claude-haiku-5-5`) |
| `TAXDIFF_LLM` | `anthropic` (default) or `ollama` with `OLLAMA_HOST` and `TAXDIFF_OLLAMA_MODEL` |
| `TAXDIFF_PARITY`, `TAXDIFF_LAW`, `TAXDIFF_DOCLING` | Live or cached outside services (see the table above) |
| `TAXDIFF_E2E_URL` | Address for the browser tests |

Optional extras: `.[parity]` (PolicyEngine-US 2.90.4), `.[docs]` (Docling), `.[llm]` (model providers),
`.[ui]` (the earlier Streamlit screen). The hosted API installs the base set pinned in `requirements.txt` from
`uv.lock`. Deployment is one Vercel project with two services (`vercel.json`): the dashboard from `web/` and the
API, with `/api/*` routed to the API.

Limits on the host: 2,000-character questions, 25 approved changes per session, 40 runs per address in 10
minutes, at most 300 model calls an hour per instance, and a 90-second deadline per run.

## API

| Endpoint | What it does |
|---|---|
| `GET /api/health` | Rule pack, book, and the state of each outside service |
| `GET /api/examples` | The example questions |
| `GET /api/book` | The 40 returns at the session start |
| `POST /api/run` | A question or change, streamed as NDJSON: `run_started`, one `node` event per step state, `result`, `run_finished` |
| `POST /api/approve` | The decision on a preview (`approve` or `reject`), streamed the same way |

## Question reader evaluation

The reader is scored on three sets of reviewer requests: pasted guidance, free-form and relative what-ifs,
stated facts, questions about a client, off-topic requests, and attempts to push an unsourced or contradicted
change through. Each case is asked of the model three times and counts only if all three runs are right.

| Set | Cases | Correct |
|---|---|---|
| Written alongside the reader | 26 | 26 |
| Written blind by an author who never saw the code | 103 | 103 |
| Held out, written later by a second author | 62 | 61 |

No wrong reading that would have run got through in any set or run. `scripts/eval_intake.py --record --runs 3`
records a new set of replies; `tests/test_intake_eval.py` replays them.

## Tests

`pytest` runs 299 tests with no key and no network call; model providers and public APIs are stubbed or replayed.

- `test_engine.py`: published 2025 Tax Table cells, Tax Computation Worksheet results, Schedule 1-A boundaries
  and the Notice 2025-69 premium rules.
- `test_workflow.py`: change 001's reach, routes and amounts; ranges hand-computed from the Tax Table; a check
  that changes each of the 29 rule parameters and one that applies 60 seeded random fact edits, with no return
  outside the reached set moving.
- `test_lanes.py`, `test_safeguards.py`, `test_edge_cases.py`: the eCFR excerpts, documents, routing boundaries,
  note figure checks, failure paths, approval of exactly the preview, exit codes.
- `test_api.py`, `test_questions.py`, `test_intake_eval.py`: every change set through the API, forged tokens and
  histories refused, 63 phrasings read by code, and the recorded model replies replayed.
- `tests/e2e/`: 32 Playwright tests that run every example, approve a preview, check a 375 px screen and reduced
  motion, and fail on any console error. Set `TAXDIFF_E2E_URL` to run them.

## Project layout

```
src/taxdiff/            engine, provenance, provisions, tax, rules, facts, changes, impact, router, workflow, cli
src/taxdiff/lanes/      intake, documents, engine_b (PolicyEngine-US), law, notes, llm
src/taxdiff/api/        FastAPI app: streamed runs, session replay, signed tokens
web/                    Next.js dashboard
rules/ty2025.yaml       rule pack v1
changes/                example change sets
data/                   synthetic book, coverage list and employer statements
cache/                  PolicyEngine-US results, Docling conversions, eCFR excerpts
docs/designs/           design document
scripts/                book and document generators, eCFR refresh, reader evaluation
tests/                  unit, workflow, API and browser tests
app/                    earlier Streamlit review screen (`pip install -e ".[ui]"`, then `streamlit run app/streamlit_app.py`)
```

## Next steps

- Keep sign-offs and the audit log on the server, with roles per return.
- Effective-dated rules and state conformity, so a change that applies from a date reaches only the returns it governs.
- More provisions and a second document type, such as a corrected W-2c read from a PDF.
- Talk to review teams about how often a change reaches prepared returns and what re-review costs today. If
  changes rarely reach filed returns, the amendment workflow matters less than the preview.
