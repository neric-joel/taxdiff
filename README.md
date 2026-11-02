# Blast Radius

**Change-impact triage for prepared tax returns.**

When a tax rule or a client fact changes after returns are prepared, Blast Radius:
- finds every affected return;
- shows which lines move and by how much;
- routes each return to the right next step, with a citation behind every number.

Returns the change cannot reach are shown as unaffected, based on what each computed line actually read.

> **Status:** in development. Setup and run instructions will be added with the first working release.

## The problem

Tax returns are computed against law and facts that keep moving after the return is prepared:
- IRS guidance arrives mid-season;
- corrected information returns arrive after filing;
- states change how they conform to federal law;
- clients remember details late.

The 2025 overtime deduction is a recent example. Only the FLSA overtime premium qualifies (IRS Notice 2025-69),
and most 2025 W-2s did not report that amount separately.

When something changes, a practice usually cannot see which of its prepared, in-review or filed returns are
affected. It either reopens everything or risks missing some.

## Approach

1. **Rules as data.** Tax parameters live in versioned rule packs, and every value cites its source.
2. **Provenance.** Each computed line records the rules and facts it read.
3. **Change sets.** Rule edits, fact edits and new documents are validated before they apply. Invalid changes
   are rejected with a reason.
4. **Impact.** Only returns whose provenance touches the change are recomputed. Results are diffed line by line.
5. **Routing.** Each affected return is routed by its status:

| Return status | Next step |
|---|---|
| In preparation | Update the return |
| In review | Back to the reviewer, with the line diff |
| Filed | Amendment review |
| A required fact is missing | Document request |

## Prototype scope

- Tax year 2025 federal individual returns (Form 1040), limited to:
  - ordinary income tax brackets;
  - the standard deduction;
  - Schedule 1-A deductions for qualified overtime, qualified tips and seniors.
- Synthetic client data only. Nothing in this repository describes a real taxpayer.
- Not modeled: AMT, capital-gain rates, credits, QBI, state returns and e-file.
