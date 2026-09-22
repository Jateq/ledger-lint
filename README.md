# ledger-lint

Extracts every reported fact from a company's SEC 10-K filing (via XBRL — the
structured data tags embedded in the filing) and checks whether the numbers
are internally consistent: do the line items that should add up to a total
actually add up? Are there tagging errors (bad signs, invalid dates,
deprecated elements)? Are there numbers in the filing that no rule ever
checks at all (orphan facts)

Built within cps-vida lab research

## What it actually does, end to end

1. **Find a filing.** Given a ticker (e.g. `AAPL`), look it up on SEC EDGAR
   and get the URL of its most recent 10-K.
2. **Extract every fact.** Load that filing into
   [Arelle](https://arelle.org/) (an open-source XBRL processor), which
   auto-downloads the filing's calculation linkbase, presentation linkbase,
   label linkbase, and taxonomy. Pull out every tagged fact: what it is,
   its value, its time period, its unit, which section of the filing it's
   in. Merge duplicate occurrences of the same fact into one, without
   losing track of where each copy came from.
3. **Build constraints.** Three independent sources of "rules a number
   should obey": the filing's own declared arithmetic (e.g. R&D expense +
   other items = Operating Expenses), the
   [XBRL US Data Quality Committee](https://xbrl.us/dqc)'s official rule
   set (sign checks, invalid dates, deprecated elements, and more), and a
   library of ~50 financial-ratio identities built from reference textbooks
   (e.g. gross profit = revenue − cost of revenue, assets = liabilities +
   equity).
4. **Check consistency.** For every arithmetic constraint (filer-calc or
   ratio-identity), does the sum of the parts actually equal the reported
   total — exactly, or at least within the rounding tolerance implied by
   how precisely each number was reported?
5. **Check global satisfiability with Z3.** Beyond checking constraints one
   at a time, can *every* complete constraint hold *simultaneously*, given
   the values the filing actually reports? SAT means the filing is
   internally consistent as a whole; UNSAT names exactly which constraints
   can't all be true together. This is also the gate a filing must pass
   before it's used as a "clean" document for bug injection (step 8).
6. **Compute the ratio library.** Alongside the identity checks, ~50
   standard financial ratios (current ratio, margins, ROE, coverage
   ratios, ...) are computed where the filing has the needed tags, each
   one tagged with which source PDF defines it and whether it fell outside
   a textbook rule-of-thumb range (advisory only — ratios are never treated
   as errors, since a ratio of 0.9 or 5 can both be normal).
7. **Find orphans.** Across the whole filing, is there any reported number
   that no constraint ever touches? Those are the numbers nothing is
   checking.
8. **Report.** One report per filing (never averaged across companies,
   since real filings vary a lot) — coverage %, consistency %, orphan
   count, Z3 status, ratio usage, and where the numbers came from.
9. **Inject a bug (`inject.py`).** Take a filing that passed the Z3 gate,
   change one reported number just enough to break a rule, and write it
   back into a byte-exact copy of the original `.htm` — producing a
   verified good/bad pair with an answer key, for testing whether an LLM
   can find and localize the planted error.
10. **Verify any file by hand (`verify.py`).** Run the same extract →
    constraints → check → Z3 pipeline against any single local `.htm` (the
    original, a bug-injected copy, or one you edited yourself) and print
    whether it's clean (SAT) or broken (UNSAT) — a separate, standalone
    tool from `inject.py`, for spot-checking a file after the fact rather
    than creating one.

## Quick start

```bash
cd ledger-lint
python3 -m venv .venv          # only needed once
source .venv/bin/activate      # do this every time you open a new terminal
pip install -r requirements.txt
python setup_dqc.py            # only needed once — see setup_dqc.py below
```

Then run the pipeline against a real company:
```bash
python run.py AAPL
```
This prints a full report to your terminal and writes it to
`reports/report_AAPL_<accession>.json`. Takes a few minutes — the DQC
validation step alone runs a large rule set against the whole filing.

## All run commands

Run these from `ledger-lint/` with the venv active (`source .venv/bin/activate`
first, every new terminal).

**Full report on a real filing:**
```bash
python run.py AAPL                          # latest 10-K (takes minutes, DQC included)
python run.py AAPL --limit 3                # 3 most recent 10-Ks, reported separately
```
Writes `reports/report_<TICKER>_<accession>.json`, `_constraints.csv`,
`_ratios.csv`, and `summary_<TICKER>.csv`.

**Bug-injection dataset** (for testing whether an LLM can find a planted error):
```bash
python inject.py AAPL                       # 1 error case
python inject.py AAPL --count 5             # 5 cases, each a different fact
python inject.py AAPL --count 5 --rank 5    # skip the first 5 candidates, get different facts
python inject.py <filing URL>               # by URL instead of ticker
```
Writes `datasets/<TICKER>/<accession>/`: `good.htm` (clean), `for_llm/case_NN.htm`
(give these to the model under test), `answers/case_NN.txt` + `.json` (keep
away from the model), `summary.json`.

**Manually check any local file:**
```bash
python verify.py datasets/AAPL/000032019325000079/for_llm/case_01.htm
python verify.py datasets/AAPL/000032019325000079/good.htm
python verify.py <file.htm> --dqc                        # also run DQC (slow)
python verify.py <file.htm> --url <original filing URL>  # only if schema files are missing
```
Prints `CLEAN (SAT)` or `ERROR FOUND (UNSAT)`.

**Individual stages** (mostly for debugging — each defaults to Apple's URL
if you give no argument):
```bash
python ratios.py AAPL      # all 52 ratios + identity checks
python solver.py [url]     # Z3 gate + smoke test
python fetch.py            # ticker -> filing URL
python extract.py          # fact extraction
python constraints.py      # filer-calc + DQC rule building
python check.py            # constraint checks
python orphans.py          # uncovered facts
python report.py           # report only
```

**Important:** your Mac likely has several Python installs (Homebrew,
system, framework builds). Arelle requires Python 3.10+, so this project's
`.venv` was built specifically from `/opt/homebrew/bin/python3` (3.14.7).
Always `source .venv/bin/activate` before running anything here — that's
what guarantees you're using the right Python with the right packages,
regardless of what `python3` means elsewhere on your machine.

## The pipeline, file by file

Files run in this order — each one feeds the next:

```
fetch.py  →  extract.py  →  constraints.py  →  check.py  →  orphans.py  →  report.py
                                    ↑
                          (setup_dqc.py is a one-time
                           setup step, not part of the
                           per-filing pipeline itself)

run.py orchestrates all of the above for one company, end to end.
```

### `fetch.py` — find the filing
**Status: done.** Takes a ticker, does nothing else but resolve it into a
URL. Three steps chained together: ticker → CIK (SEC's internal company ID,
via a static ticker-lookup file SEC publishes) → list of that company's
10-K filings (via SEC's per-company filings feed) → the direct URL of one
filing's main document. Sends SEC's required descriptive `User-Agent`
header on every request. Makes no more than 2 HTTP requests per lookup, so
it's naturally well under SEC's 10-requests/second limit — this becomes
something to watch once we're looping over many companies at once, not a
concern for single lookups today.

### `extract.py` — pull every fact out of the filing
**Status: done.** Takes one filing URL (from `fetch.py`) and hands it to
Arelle, which downloads and parses the filing plus everything it
references (linkbases, taxonomy). From the loaded filing, pulls out every
fact as a clean record: `concept` (what kind of thing — e.g. "Net Income"),
`value`, `unit`, `decimals` (how precisely it was reported — this is where
rounding tolerance comes from later), `context_ref` (decoded into plain
entity/period/dimensions instead of a coded ID), `label` (human-readable
name), `sections` (which part of the filing it appears in, from the
presentation linkbase), and `type` (numeric vs. text/date). Numeric facts
that are really the same fact reported twice (same concept, same real
period, same unit) get merged into one, while every original occurrence
stays linked under `occurrences` — nothing is silently thrown away. Reports
three counts every time: total fact occurrences, numeric fact occurrences,
and unique numeric facts after merging duplicates.

*Known gap, not yet fixed:* if two occurrences that get merged together
actually disagree on value, that should be flagged as a conflict rather
than silently resolved — not implemented yet.

### `run.py` — the orchestrator
**Status: done — runs the entire pipeline.** This is the file you actually
run. Give it a ticker, and it does everything: resolve the filing, extract
every fact, build constraints, check them, find orphans, and write a full
report — no copying anything between files by hand. `python run.py AAPL`
processes the single most recent 10-K; `python run.py AAPL --limit 3`
processes the 3 most recent, each one **reported on separately** —
filings are never pooled together, since variation between companies and
years is expected and meaningful. Writes one `report_<TICKER>_<accession>.json`
per filing.

### `setup_dqc.py` — one-time environment setup, not part of the pipeline
**Status: done.** The XBRL US Data Quality Committee's real rule engine
isn't a `pip install` — it's a separate Arelle plugin (`xule`, from a
different GitHub project) whose own code only works if it's physically
copied into Arelle's installed plugin folder. Since `.venv` isn't tracked
by git, that manual copy wouldn't survive a fresh clone or a rebuilt
virtual environment. This script reproduces that setup automatically:
clones the plugin source, copies the two pieces Arelle needs
(`xule/` and `validate/DQC.py`) into the currently-active virtual
environment's Arelle installation, and verifies it activates. Run it once,
any time you set up (or reset) `.venv`. It does **not** install the actual
DQC rules — those get downloaded automatically by the plugin itself, the
first time it validates a filing that needs them.

### `constraints.py` — build the rules a filing's numbers should obey
**Status: done.** Produces two kinds of constraints per filing:
**filer-calc** (arithmetic relationships the company itself declared,
e.g. "these expense lines sum to Operating Expenses", read from the
filing's calculation linkbase, then instantiated against every real
period the total concept actually has a value for) and **DQC** (the
official rule set, run for real via the plugin `setup_dqc.py` installs —
shells out to Arelle as a subprocess since the DQC plugin only has a
command-line interface, not a documented in-process API). Each constraint
is tagged with `rule_source` and `spans_sections` (true only if there's
no single section containing *all* of that constraint's facts). Also
tracks **failures**: filer-calc relationships that couldn't be built
because a required fact is missing or nil, and a DQC run that didn't
complete cleanly (timeout/crash) — kept separate from genuine
inconsistencies. Tested on Apple's FY2025 10-K: 168 filer-calc
constraints instantiated, 2 real DQC violations found and correctly
linked back to the specific fact each is about.

### `check.py` — is each constraint actually satisfied?
**Status: done.** DQC constraints already carry their verdict (a DQC
finding *is* a failure) so they pass through untouched. For every
filer-calc constraint: computes the residual (sum of the parts minus the
reported total), the propagated rounding band (each involved fact's own
tolerance, from its `decimals`, combined — not just one fact's precision
in isolation), and pass/fail under both zero tolerance and rounding-aware
tolerance. Also computes `min_injectable_delta` — the smallest change to
the most sensitive fact that would push a passing constraint into
failing, a measure of how fragile each check actually is. On Apple's
filing: 168/168 filer-calc constraints reconcile exactly (zero residual),
no rounding room even needed.

### `orphans.py` — which facts does nothing check?
**Status: done.** After constraints (filer-calc + DQC) have been built
across the *entire* filing — not one section at a time, since a
constraint can require facts from anywhere — flags every unique numeric
fact that never participates in a single constraint
(`is_orphan_in_filing`). On Apple's filing: 341 of 880 unique numeric
facts (38.75%) are orphans, including things like cover-page metadata
(never part of any total) and segment-level revenue breakdowns that
Apple's calculation linkbase doesn't declare a roll-up relationship for.

### `report.py` — one summary per filing
**Status: done.** Pulls together everything above into one report per
filing: fact counts, coverage % of unique numeric facts by at least one
rule, constraint counts by `rule_source`, consistency (exact vs.
rounding-only vs. failing) for filer-calc, DQC violation count, % of
constraints spanning sections, and the extraction/rule-execution failures
list. Prints a readable summary and writes the full report as JSON.
Filings are always reported individually, never averaged together.

### `solver.py` — can every constraint hold at once? (Z3)
**Status: done (satisfiability gate; used by `inject.py`).**
`check.py` tests constraints one by one; `solver.py` asks Z3 whether they
can all hold together given the values the filing reports. Every unique
numeric fact becomes a Z3 variable pinned to its reported value, and every
complete filer-calc or ratio-identity constraint becomes a rounding-band inequality. **SAT**
means the filing is internally consistent and safe to use as a "clean"
document; **UNSAT** returns the unsat core (the constraints that can't all
hold). Partial constraints are left out (they can be context-matching
artifacts) and DQC findings add no equations. `python solver.py [url]`
also runs a smoke test: shift one total outside its band and confirm Z3
flips to UNSAT and names the broken constraint.

### `ratios.py` — ratio library from the reference PDFs (Workstream B)
**Status: done.** `python ratios.py AAPL` prints every ratio for the latest
10-K. Two separate jobs:

1. **Ratios (52).** Formulas from the CFA Level II list and the Duke FSA
   note (read as text), cited also by page in the CFI and Gillingham books
   (their formulas are images, so only "which page discusses it" is recorded).
   A ratio is **never** treated as an error, since a current ratio of 0.9
   or 5 can both be normal. Per filing each ratio is `computed` or
   `not_usable` with a reason: `missing_terms` (the filing has no matching
   tag), `needs_prior_period` (average-balance ratios need last year's
   balance), `no_standard_xbrl_concept` (e.g. daily cash expenditures) or
   `not_in_filing` (stock price for P/E). Every result lists the exact
   facts (concept, fact id, value) used and the PDF(s) that define it.
   Rule-of-thumb ranges (Duke) are flagged as **advisory only**. Basic EPS
   computed from net income and weighted shares is also compared with the
   EPS the filing reports (within rounding): a real cross-check.
2. **Identities (5 rules, ~13 instances per filing).** Equalities the
   ratio definitions rely on: gross profit = revenue - cost of revenue,
   operating income = gross profit - operating expenses, net income =
   pretax income - tax, total assets = liabilities and equity, and
   liabilities and equity = liabilities + equity (including non-controlling
   and redeemable interests when reported; the most specific variant whose
   lines are all reported is used). These are emitted as constraints with
   `rule_source: "ratio-identity"`, so `check.py`, `solver.py` (Z3),
   `orphans.py`, `inject.py` and `verify.py` treat them like filer-calc
   rules; `report.py` lists them separately from the filer-calc headline.

The term-to-concept mapping (`TERMS` in the file, e.g. "cash" -> first of
`CashAndCashEquivalentsAtCarryingValue`, `Cash`) is ours, not from any PDF.
Approximations to know about: EBIT is taken as operating income; total debt
is commercial paper + current debt + non-current debt; interest expense is
used where the CFA list says interest payments.

### `inject.py` — make provably-bad copies of a provably-clean filing
**Status: done for filer-calc and ratio-identity constraints (fixed offset just past the rounding band).**
`python inject.py AAPL --count 5` (a ticker or a filing URL; `--count N` =
how many different errors, default 1). Requires the clean filing to be SAT.
For each case it picks a different fact used by ≥2 complete constraints
(and a different broken rule), raises its magnitude just past the tightest
rounding band, and has Z3 predict UNSAT plus the unsat core. It copies the
original `.htm` and changes only the text inside that fact's `ix:nonFraction`
tag(s), in every place the fact is printed. Each case is then re-parsed with
Arelle and Z3 and must be UNSAT with the predicted core; the clean file must
be SAT. Only `ixt:num-dot-decimal` numbers are editable.

Output, `datasets/<TICKER>/<accession>/`:
- `good.htm` — clean original (SAT)
- `for_llm/case_NN.htm` — corrupted files to feed the model under test
- `answers/case_NN.txt` / `.json` — answer keys (keep away from the model)
- `summary.json` — one row per case, with verification status
- `_support/` — schema + linkbase files Arelle needs for local copies

**How a fact is identified.** In inline XBRL every number is tagged
`<ix:nonFraction name="..." contextRef="..." unitRef="..." id="...">`. A
`contextRef` (e.g. `c-18`) is defined once per (entity, period, dimensions)
combination and then reused by every tag that needs it — 105 different
concepts in Apple's filing all point at `c-18` for "FY2024, whole company,
no segment breakdown." So the same concept (e.g. `OperatingIncomeLoss`) can
appear at many different `contextRef`s (one per year, and again per segment
if it's broken out), and each of those is a genuinely different number.

The **unique key for one specific entry is `name` + `contextRef` +
`unitRef` together** — this is exactly `extract.py`'s dedup key
(`numeric_dedup_key`, `extract.py:94`). Two tags with the same key are not
two different facts, they're the *same* fact printed twice (e.g. once in
the income statement, once again in a footnote); `extract.py` links them
together under one fact's `occurrences` list of tag `id`s. `id` itself is
never part of the key — it only locates one specific spot in the HTML.

`inject.py` uses this: `rank_candidates` picks a fact by its unique key
(so it always targets one specific concept in one specific year), then
`plan_injection` looks up every `id` in that fact's `occurrences` and edits
all of them to the same new number, so the document stays internally
consistent. Each answer key names the exact tag `id`s, the `contextRef`,
and the fiscal period, and gives a plain "search for `id="f-103"`, it
should say X but shows Y" line so the injected number can be found by eye
without running any code.

### `verify.py` — manual full check of one local file
`python verify.py datasets/AAPL/<accession>/for_llm/case_01.htm [--dqc]`.
Runs extract → constraints → exact and rounding-band math → Z3, and prints
`CLEAN (SAT)` or `ERROR FOUND (UNSAT)` (exit code 0/1). `--dqc` adds the slow
DQC pass.

## Supporting files

- **`requirements.txt`** — pinned Python package versions
  (`requests`, `arelle-release`, `aniso8601`). Install with
  `pip install -r requirements.txt`.
- **`.venv/`** — the project's isolated Python environment (Python 3.14.7).
  Not tracked by git (see `.gitignore`) — rebuild it with the Quick Start
  steps above if it's ever missing.
- **`reports/`** — everything `run.py` generates lands here, per run:
  `report_<TICKER>_<accession>.json` (full report),
  `report_<TICKER>_<accession>_constraints.csv` (one row per constraint:
  residual, rounding band, pass/fail under both tolerance modes, headroom,
  `min_injectable_delta`, `spans_sections`), and `summary_<TICKER>.csv`
  (one row per filing with the headline numbers — rows are never pooled).
  Not tracked by git — regenerate any time by re-running the pipeline.
- **`.gitignore`** — keeps `.venv/`, `__pycache__/`, compiled `.pyc`
  files, and `reports/` out of git.

## Where things stand right now

The full pipeline runs end to end. `python run.py AAPL` was tested against
Apple's real FY2025 10-K and produced a complete report:

| Step | File | Status |
|---|---|---|
| Find filing | `fetch.py` | ✅ done, tested against real Apple filings |
| Extract facts | `extract.py` | ✅ done — one known gap: conflicting-duplicate-value flagging |
| DQC engine setup | `setup_dqc.py` | ✅ done, tested — found real DQC violations in Apple's 10-K |
| Build constraints | `constraints.py` | ✅ done, tested — 168 filer-calc + 2 real DQC + 13 ratio-identity constraints |
| Check consistency | `check.py` | ✅ done, tested — 168/168 filer-calc pass exactly, 13/13 ratio-identity pass |
| Detect orphans | `orphans.py` | ✅ done, tested — 613/880 facts covered (267 orphaned) |
| Generate report | `report.py` | ✅ done, tested — full JSON + readable summary |
| Orchestrate everything | `run.py` | ✅ done — ticker in, `report_<TICKER>_<accession>.json` (+ constraints and ratios CSVs) out |
| Z3 gate | `solver.py` | ✅ done — SAT on clean Apple and Tesla |
| Ratio library | `ratios.py` | ✅ done — 52 ratios, 5 identity rules, EPS cross-check |
| Bug injection dataset | `inject.py`, `verify.py` | ✅ done — N verified cases per filing, answer keys |

**Known gaps / not yet handled:**
- Filer-calc constraints where some declared children aren't reported at
  the total's context are marked `partial` and reported separately from
  the headline pass rate, because they can be artifacts (e.g. a total
  reported with no dimensions whose children only exist under a
  dimension, as seen on Tesla's `Assets`). Whether such cases are real
  errors or an artifact of context matching hasn't been settled.
- Merged duplicates whose values disagree are flagged (`has_conflict`,
  with `conflict_within_rounding` for gaps explained by precision), but
  what to do with real conflicts beyond flagging is undecided.
- Only two constraint sources exist (`filer-calc`, `DQC`) — a third
  `custom` source (hand-built rule templates, e.g. domain-specific ratio
  checks like the EPS example in Asmaa's reply) was intentionally left out
  of this first pass; Asmaa's guidance was to try Arelle + DQC across
  complete filings first and revisit custom rules based on what coverage
  actually looks like.
- Only tested against Apple so far — running against other companies may
  surface new edge cases (that's expected and part of why per-filing,
  non-pooled reporting matters).
