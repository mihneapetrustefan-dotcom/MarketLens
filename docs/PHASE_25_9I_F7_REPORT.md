# Phase 25.9I-F.7 — Completion Report

> **RECOVERED HISTORY CREATES A NEW DATASET VERSION. IT DOES NOT REWRITE THE PAST
> EXPERIMENT.**
>
> **MORE HISTORICAL ROWS ARE USEFUL ONLY IF THEIR TEMPORAL AND PROVENANCE
> INTEGRITY IS VALID.**

Written 2026-10-01. Architecture: `docs/PHASE_25_9I_F7_ARCHITECTURE.md`.

---

## 1. Phase status

**COMPLETE.**

---

## 2. Dataset recovery status

**RECOVERED AND VALIDATED.**

A separate judgement from §1, as required. The recovery ran to completion, every
recovered record passed the temporal-integrity contract, the source database is
provably unchanged, and the new corpus and snapshot are frozen with verified
content hashes.

---

## 3. F.6 closure check

F.6's documentation was missing when this phase began, so it was finished first
and nothing else was started until it was.

| | |
|---|---|
| F.6 architecture present? | **YES** — `docs/PHASE_25_9I_F6_ARCHITECTURE.md`, written in this session |
| F.6 completion report present? | **YES** — `docs/PHASE_25_9I_F6_REPORT.md`, all 35 required sections |
| F.6 result immutable? | **YES** — `IMPLEMENTATION = COMPLETE`, `MODEL QUALIFICATION = NONE QUALIFIED (INSUFFICIENT DATA)`, unchanged and not reinterpreted |
| Pre-registration recorded? | **YES** — `ed016e8`, committed before `1be725d` ran any candidate |
| Old holdout unchanged? | **YES** — `2026-08-15T01:23:31Z`, still the boundary for snapshot v1 |
| Full regression after F.6? | **YES** — 5,119 tests, OK (skipped=1), 488.078 s at `1be725d` |
| Documentation commit | `3dbfde7`, documentation only: 2 files, 0 code changes |

F.6's scientific result was not touched. §21 below proves its stored evaluation
still references the data it actually used.

---

## 4. Git state

| | |
|---|---|
| Branch | `ibkr-paper-validation-fixes` |
| Baseline HEAD | **`3dbfde7`** (F.6 documentation) |
| Final HEAD | **`<F.7 commit>`** |
| Working tree at start | clean |
| Commits | 1 (plus `3dbfde7` closing F.6 first) |
| Diff | **15 files changed, 7,236 insertions(+), 2 deletions(-)** |
| **Files modified** | **2** |

### New files (13)

| File | Lines | Purpose |
|---|---|---|
| `src/domain/corpus_recovery_models.py` | 1,047 | the contracts, and the refusals |
| `src/adaptive/corpus_funnel.py` | 658 | funnel, loss accounting, stage audits |
| `src/adaptive/corpus_versioning.py` | 695 | versions, manifests, provenance, lineage |
| `src/adaptive/research_snapshot.py` | 378 | holdout rule, feasibility, handoff |
| `scripts/audit_historical_corpus.py` | 250 | read-only diagnostic |
| `scripts/recover_historical_corpus.py` | 949 | plan / validate-slice / run |
| `scripts/freeze_research_handoff.py` | 321 | inspect / freeze |
| `tests/adaptive/test_corpus_recovery.py` | 715 | 71 tests |
| `tests/adaptive/test_research_snapshot_integrity.py` | 627 | 55 tests |
| `tests/adaptive/test_recovery_boundary.py` | 432 | 30 tests |
| `docs/PHASE_25_9I_F6B_PREREGISTRATION.md` | 300 | the next phase's frozen plan |
| `docs/PHASE_25_9I_F6B_HANDOFF.json` | 223 | the machine-readable handoff |
| `docs/PHASE_25_9I_F7_ARCHITECTURE.md` | 606 | documentation |

### Modified files (2)

| File | Change | Why |
|---|---|---|
| `.gitignore` | +11 | the recovered database is a ~196 MB Release asset, not a git file; committing it would break the 100 MB limit |
| `tests/adaptive/test_freshness_and_legacy.py` | +24/−2 | an older test's substring proxy was narrowed to the package it actually guards — see §15.5 |

No production module was modified. **`data/marketlens.db` was not written to at
all.**

---

## 5. Baseline tests

Recorded **before** any F.7 change, at `1be725d` (code-identical to the `3dbfde7`
baseline HEAD, which changed only documentation):

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5119 tests in 488.078s
OK (skipped=1)
```

| | |
|---|---|
| Tests | **5,119** |
| Pass | **5,118** |
| Fail | **0** |
| Errors | **0** |
| Skipped | **1** |
| Runtime | **488.078 s** |

### Baseline historical coverage, measured read-only

| | Value |
|---|---|
| Earliest raw article | **2026-07-01T22:25:59Z** |
| Latest raw article | 2026-08-30T21:09:11Z |
| Raw article count | **48,392** (`raw_articles` is empty; `articles` is the raw layer) |
| Earliest normalized article | 2026-07-01T22:25:59Z |
| Normalized article count | **48,392** (`news_articles`) |
| Earliest entity-resolved article | **2026-07-30T13:04:26Z** |
| Entity-resolved article count | **23,504** (26,400 links) |
| Earliest event report | **2026-07-30T13:34:32Z** |
| Event report count | **846** |
| Earliest canonical event | **2026-07-30T13:34:32Z** |
| Canonical event count | **581** |
| Earliest research observation | **2026-07-30T13:34:32Z** |
| Research observation count | **581** |

A note on method, because it nearly went wrong in F.6 and would have again: the
project has no `pytest`, and invoking it returns exit code 0 while printing
`No module named pytest`. Every test figure in this report comes from
`unittest discover`.

---

## 6. Original historical coverage

See the table in §5. The single fact the phase turns on:

**Articles begin 2026-07-01. Entity links begin 2026-07-30T13:04:26Z. Every stage
after entity resolution begins within 30 minutes of that instant.**

The corpus did not start late. The entity stage did.

---

## 7. Root cause

**Mandatory section. Evidence only.**

### The cause

`scripts/backfill_article_entities.py` accepts a `--since` argument and applies it
as a publication floor:

```python
if since:
    where.append("published_at >= ?")
    params.append(since)
```

It was run with that bound set at approximately **2026-07-30T13:04Z**.
**17,789 articles published before it were never scanned by entity resolution.**

`scripts/populate_events.py` then selects only
`a.article_id IN (SELECT DISTINCT article_id FROM article_entities)`, so an
article with no entity link can never produce an event. No event ⇒ no canonical
event ⇒ no research observation. The entire first month of the corpus was absent
from research, and nothing downstream recorded a reason for it, because nothing
downstream was ever offered the rows.

`populate_events.py` carries the same `--since` flag — a second, redundant
instance of the same hazard.

### Why it is a date filter and not anything else

| Hypothesis | Refuted by |
|---|---|
| different kind of source | **17,760 of 17,789** pre-cutoff articles come from the same `Google News: <Company>` feeds that reach 92% coverage after the cutoff |
| missing mention data | **15,518 of 17,789** have populated `companies_mentioned`, e.g. `[{"company": "Boeing", "ticker": "BA", ...}]` at 2026-07-30T13:02:21Z — two minutes before the cutoff |
| ingestion cutoff | all 48,392 articles are present in both `articles` and `news_articles` |
| collection-time effect | in the **2026-08-23 collection run alone**, 16,248 articles published after the cutoff produced 14,696 entity links, and **9,767 published before it produced 0** — same run, same code, same sources, same day |
| normalization omission | normalization loses **0** articles |
| fusion scoped narrowly | fusion loses **0** reports; `fuse_events.py` has no date argument |
| canonical persistence bug | the canonical stage loses **0** |
| sparse historical coverage | **29 consecutive days** with 100–1,400 articles each and **exactly zero** entity links — a step, not a taper |

### Decisive measurement

Entity coverage, split on the measured cutoff:

| | Articles | With mention data | Entity-linked | Coverage |
|---|---|---|---|---|
| **before** 2026-07-30T13:04:26Z | 17,789 | 15,518 (87.2%) | **0** | **0.0%** |
| **from** 2026-07-30T13:04:26Z | 30,603 | 22,993 (75.1%) | 23,504 | **76.8%** |

The pre-cutoff side has *higher* mention density and *zero* coverage. A resolver
does not behave that way.

### Daily transition

| Day | Articles | Entity-linked | % | Events |
|---|---|---|---|---|
| 2026-07-27 | 962 | 0 | 0.0 | 0 |
| 2026-07-28 | 1,474 | 0 | 0.0 | 0 |
| 2026-07-29 | 1,331 | 0 | 0.0 | 0 |
| **2026-07-30** | 1,395 | **321** | **23.0** | **10** |
| 2026-07-31 | 1,251 | 1,162 | 92.9 | 29 |

The boundary falls **mid-day** on 2026-07-30: the last unlinked article published
at 13:03:04Z, the first linked one at 13:04:26Z.

---

## 8. Historical funnel

Counted in **distinct articles reaching each stage**, so it reconciles and each
drop belongs to exactly one stage.

### Corpus v1 (before recovery)

| Stage | Entering | Exiting | Drop | Drop % | Earliest |
|---|---|---|---|---|---|
| raw article | 48,392 | 48,392 | 0 | 0.0 | 2026-07-01 |
| normalized | 48,392 | 48,392 | 0 | 0.0 | 2026-07-01 |
| **entity resolved** | 48,392 | **23,504** | **24,888** | **51.43** | **2026-07-30** |
| event report | 23,504 | 825 | 22,679 | 96.49 | 2026-07-30 |
| fused | 825 | 825 | 0 | 0.0 | 2026-07-30 |
| canonical event | 825 | 825 | 0 | 0.0 | 2026-07-30 |
| research observation | 825 | 825 | 0 | 0.0 | 2026-07-30 |

`reconciles = True`. First material break: **entity resolution**.

### Corpus v2 (after recovery)

| Stage | Entering | Exiting | Drop | Drop % | Earliest |
|---|---|---|---|---|---|
| raw article | 48,392 | 48,392 | 0 | 0.0 | 2026-07-01 |
| normalized | 48,392 | 48,392 | 0 | 0.0 | 2026-07-01 |
| entity resolved | 48,392 | **39,706** | 8,686 | **17.95** | **2026-07-01** |
| **event report** | 39,706 | **1,509** | 38,197 | 96.2 | **2026-07-02** |
| fused | 1,509 | 1,509 | 0 | 0.0 | 2026-07-02 |
| canonical event | 1,509 | 1,509 | 0 | 0.0 | 2026-07-02 |
| research observation | 1,509 | 1,509 | 0 | 0.0 | 2026-07-02 |

`reconciles = True`. The first material break **moved** from entity resolution to
event extraction — that is, the remaining drop is now the legitimate "this
article reports no event" drop, which is what a working pipeline looks like.

### Record counts and temporal coverage

| | v1 | v2 |
|---|---|---|
| articles | 48,392 (2026-07-01 → 08-30) | 48,392 (unchanged) |
| entity links | 26,400 | **44,597** |
| event reports | 846 (2026-07-30 → 08-27) | **1,541 (2026-07-02 → 08-30)** |
| fusion decisions | 846 | **1,541** |
| canonical events | 581 (2026-07-30 → 08-27) | **1,056 (2026-07-02 → 08-30)** |
| research observations | 581 | **1,056** |
| research features | 19,103 | **34,397** |
| research labels | 9,192 | **16,116** |

### Processing times, which is how the batch rebuilds were visible

| | Range | Distinct days | Elapsed |
|---|---|---|---|
| `articles.collected_at` | 2026-08-04 → 2026-08-30 | 26 | — |
| `events.detection_time` (v1) | 2026-08-28T11:33:37 → 11:33:38 | **1** | **0.672 s** |
| `research_observations.observation_created_at` (v1) | 2026-08-28T21:42:54 → 21:42:54 | **1** | **0.119 s** |

846 events detected in 0.672 seconds on a single day. That is one batch rebuild,
and seeing it next to its information-time range is what made the date filter the
obvious hypothesis to test.

---

## 9. Where records were lost

**First material break: entity resolution, 2026-07-30T13:04:26Z, 100% loss on the
earlier side.**

Loss accounting over all 48,392 source articles. **Reconciles exactly:
0 unclassified.**

| Reason | All | Before the cutoff | From the cutoff |
|---|---|---|---|
| became canonical-event evidence | 825 | **0** | 825 |
| legitimate no-event article | 22,662 | **0** | 22,662 |
| **entity resolution never attempted** | **17,789** | **17,789** | **0** |
| entity resolution attempted, unresolved | 7,099 | **0** | 7,099 |
| extraction never attempted | 17 | **0** | 17 |

The pre-cutoff column contains exactly **one** reason, and it is the recoverable
one.

### Recoverable, honestly bounded

| | |
|---|---|
| never offered to entity resolution | **17,789** |
| of those, carrying resolvable mention data | **16,032 (90.1%)** |
| of those, with nothing in the row to resolve | **1,757** |

`ENTITY_UNRESOLVED` and `ENTITY_AMBIGUOUS` are classified as **not recoverable**,
deliberately. The resolver looked at those and declined; re-running it unchanged
gives the same answer, and loosening it to change that answer is forbidden — a
false mapping is worse than a missing one.

---

## 10. Entity resolution audit

### v1, either side of the measured cutoff

| | Pre-cutoff | Post-cutoff |
|---|---|---|
| articles | 17,789 | 30,603 |
| with mention data | 15,518 | 22,993 |
| mention density | **87.2%** | **75.1%** |
| entity-linked | **0** | 23,504 |
| entity coverage | **0.0%** | **76.8%** |
| entity links | 0 | 26,400 |
| distinct companies mapped | 0 | — |
| `Google News:` company-feed articles | **17,760** | — |
| distinct sources | 386 | 397 |
| mention density comparable | **True** | |
| coverage gap | **+76.8 percentage points** | |

Same source families, comparable mention density, 76.8 points of coverage
difference. The difference is not in the news.

### v2, after recovery

| | Value |
|---|---|
| entity cutoff | **2026-07-01T22:25:59Z** — the first article in the corpus |
| articles before the cutoff | **0** (the skipped region no longer exists) |
| entity coverage, whole corpus | **82.1%** (39,706 of 48,392) |
| entity links | **44,597** |
| distinct companies mapped | **381** |

### Resolution quality — not loosened

Measured over all 48,392 articles with no date bound:

| Status | Mentions | Share |
|---|---|---|
| resolved | 56,806 | **99.6%** |
| high confidence | 72 | 0.1% |
| unresolved | 158 | 0.3% |
| **ambiguous** | **8** | **0.0%** |

No matching rule, alias, threshold or confidence requirement was changed. Only
confident resolutions are persisted, exactly as before. The high rate is because
the mention columns were already structured — the resolver never needed help, it
needed to be asked.

### Monthly coverage, before and after

| Month | Articles | v1 entity | v1 % | **v2 entity** | **v2 %** | v1 canonical | **v2 canonical** |
|---|---|---|---|---|---|---|---|
| 2026-07 | 19,383 | 1,483 | **7.7** | **17,507** | **90.3** | 37 | **514** |
| 2026-08 | 29,009 | 22,021 | 75.9 | 22,199 | 76.5 | 544 | 542 |

July's coverage went from 7.7% to 90.3%, and is now higher than August's.

---

## 11. Extraction audit

| | v1 | v2 |
|---|---|---|
| extraction cutoff | 2026-07-30T13:34:32Z | **2026-07-02T07:00:00Z** |
| eligible articles | 23,504 | **39,706** |
| attempted | 23,487 | **39,700** |
| **skipped** | **17** | **6** |
| successful (produced an event) | 825 | **1,509** |
| no event found | 22,662 | 38,191 |
| failed / errors | **0** | **0** |
| event reports | 846 | **1,541** |
| extraction tier | `deterministic_rule` (846) | `deterministic_rule` (**1,541**) |
| status | `detected` (846) | `detected` (**1,541**) |

**Extraction was never run on the historical region — it was not "run and
failed".** Those are different problems and this is the first: zero extraction
errors exist in either version, and the eligible population itself was short
because entity resolution upstream had not produced the links extraction selects
on.

Every event in both versions is `deterministic_rule`, which is how the phase
knows no paid API or LLM was ever involved.

---

## 12. Fusion audit

| | v1 | v2 |
|---|---|---|
| reports eligible | 846 | **1,541** |
| reports fused | 846 | **1,541** |
| **reports not fused** | **0** | **0** |
| canonical events created | 581 | **1,056** |
| duplicates collapsed | 265 | **485** |
| fusion decisions | 846 | 1,541 |
| &nbsp;&nbsp;`different_event` | 483 | 863 |
| &nbsp;&nbsp;`same_event` | 265 | 484 |
| &nbsp;&nbsp;`possible_same_event` | 97 | 191 |
| &nbsp;&nbsp;`needs_review` | 1 | 3 |
| review cases | 98 | 194 |
| contradictions | 0 | **1** |
| fusion errors | **0** | **0** |

**Fusion was never the break.** It loses nothing in either version and has no
date argument. It was audited because the brief asked and because assuming would
have been the wrong method, not because it was suspected.

One `contradicted` canonical event appeared in v2 and none existed in v1. That is
fusion doing its job on a larger corpus, and it is recorded rather than smoothed.

---

## 13. What was implemented

**Diagnostics** — `src/adaptive/corpus_funnel.py`: the article-unit funnel with
reconciliation and first-material-break detection, loss accounting over 17
classified reasons, the measured stage cutoffs, time distribution with hard-cutoff
detection, and the entity, extraction, fusion and price audits.
`scripts/audit_historical_corpus.py` is the read-only front end.

**Root-cause fix** — no code was patched. The fix is to **run the existing stages
without `--since`**, and a test asserts neither `--since` nor `--limit` appears in
any stage invocation, so it cannot be reintroduced.

**Recovery runner** — `scripts/recover_historical_corpus.py`, with `--plan`
(default, writes nothing), `--validate-slice` and `--run`. It sequences the six
existing pipeline scripts, prepares the target, records provenance, verifies
temporal integrity, verifies the source is unchanged, and freezes the result.

**Versioning** — `src/adaptive/corpus_versioning.py`: five registry tables, all
`CREATE TABLE IF NOT EXISTS`; the `BUILDING → VALIDATED → FROZEN →
ELIGIBLE_FOR_RESEARCH` lifecycle; and immutability enforced at both the domain and
persistence layers.

**Manifests** — content hashes over sorted identities plus information-time
fields, canonicalised as JSON. Deterministic by construction, and computable
without reading any label value.

**Lineage** — `recovery_provenance` (3,653 rows) and `cross_version_event_map`
(1,054 rows), matched by fingerprint.

**Timestamp handling** — `InformationTiming`, which refuses an information time at
or after the recovery instant, applied to every recovered record before the
corpus may be frozen.

**Snapshot builder** — the existing `build_research_observations.py` and
`compute_features.py`, reused unchanged against the new file.

**Pre-registration foundation** — `src/adaptive/research_snapshot.py` (the
inherited holdout rule, fold feasibility, eligibility, adequacy, handoff),
`scripts/freeze_research_handoff.py`, `docs/PHASE_25_9I_F6B_PREREGISTRATION.md`
and `docs/PHASE_25_9I_F6B_HANDOFF.json`.

**Tests and docs** — **156 new tests** (71 + 55 + 30); the architecture document
and this report.

---

## 14. What worked

Evidence-backed only.

1. **The root cause was proven, not guessed.** A single collection run on
   2026-08-23 produced 14,696 entity links for articles published after the
   cutoff and **0** for 9,767 published before it. Same run, same code.
2. **The funnel reconciles exactly** in both versions, 0 unclassified of 48,392,
   and the first material break is identified rather than inferred.
3. **The replay reused every existing stage.** No second pipeline; a test asserts
   the recovery imports no resolver, extractor, fusion engine or study engine.
4. **Idempotency was proven, not asserted.** All six stages replayed a second
   time: 0 new rows, byte-identical corpus and snapshot manifest hashes.
5. **Temporal integrity held across 3,653 records with 0 violations**, and the
   contract would have failed the run rather than warn.
6. **The source database is provably unchanged.** Corpus hash
   `99777b5971b088a9…` and snapshot hash `1d0010564b85abea…` identical before and
   after, recomputed from the untouched file.
7. **The recovery cost nothing.** 0 paid API calls, 0 LLM calls, 0 network calls,
   enforced by import tests.
8. **The validation slice came first**, was chosen by position in the gap rather
   than by returns, and its lineage was walked by hand before the full run.
9. **The constraint F.6 identified is gone.** Research region 15 days → **46
   days**; every daily horizon now forms 3–5 folds where all four formed 0.
10. **The holdout rule was inherited, not chosen.** Phase 25.9's 20%, applied
    mechanically, with the refusal encoded in a contract.
11. **Zero regressions.** 5,119 → 5,275 tests, all passing.

---

## 15. What did not work

**Mandatory, and complete.** Every item is a real failure encountered during this
phase.

### 15.1 Three test failures on the first run of the new suites

- **Symptom:** 3 of 154 new tests failed.
- **Root cause:** two were my tests being wrong about my own code (15.2, 15.3);
  one was my test being wrong about the data (15.4).
- **Status:** all resolved, and in two of the three cases the test changed rather
  than the code, because the code was right.

### 15.2 A boundary test flagged its own documentation

- **Symptom:** `test_no_f7_file_names_an_order_or_broker_symbol` failed on
  `OrderIntent` appearing in `recover_historical_corpus.py`.
- **Root cause:** the string appears in the module docstring, in the sentence
  *"creates no OrderIntent"*. A blunt substring search over the whole file flags
  the explanation of the prohibition.
- **Fix:** added `code_only()`, which parses the file and strips docstrings,
  comments and string literals, so identifier prohibitions are checked against
  code; and `without_prose()`, which keeps non-docstring strings so embedded SQL
  is still checkable. The structural assertions are unchanged.
- **Status:** resolved. Phase 25.9I-F.5 hit the identical trap with a seal test;
  it is now fixed in a reusable way rather than by rewording a comment.

### 15.3 A staleness test forbade the word it was explaining

- **Symptom:** `test_a_stale_stored_evaluation_cannot_define_the_new_capacity`
  failed because `research_snapshot.py` contains `effective_sample_size`.
- **Root cause:** it appears in the module docstring explaining F.6's root cause.
- **Fix:** the assertion now runs against the unparsed AST with docstrings
  blanked.
- **Status:** resolved.

### 15.4 A test asserted an invariant that was not one

- **Symptom:** `test_a_canonical_event_is_never_first_reported_before_its_article`
  failed on **411 of 1,541** evidence links.
- **Root cause:** **my test was wrong.** `first_reported_at` means the *earliest*
  report, so corroborating articles published later than it is correct behaviour.
  The same pattern exists in v1 (247 of 846), so it is pre-existing and right.
- **Fix:** replaced with the invariant that actually matters — `first_reported_at`
  must equal the **minimum** `published_at` across its evidence — plus a companion
  test that no evidence predates the first report. Both pass with 0 violations.
- **Status:** resolved. Worth recording because a 411-row "violation" looked
  alarming and was entirely my misunderstanding of the field.

### 15.5 An older D20 test blocked a legitimate import

- **Symptom:** `test_the_protected_ledger_is_not_imported_by_this_phase` failed
  with *"`corpus_versioning.py` imports `src.domain.research_models`"*. It passed
  when the F.7 files ran alone and failed in the full suite, because it scans the
  whole `src/adaptive` directory.
- **Root cause:** the test asserted *no imported module name may contain the word
  "research"*. That is a proxy, not the rule. The protected ledger is
  `src/research/protected_ledger.py`; `src.domain.research_models` holds only
  generic contracts — `DatasetVersion`, `FeatureValue`, `LabelValue`,
  `ResearchObservation` — shared by the whole pipeline, and carries no protected
  outcome.
- **Fix:** narrowed the check to the package it guards (`src.research`,
  `protected_ledger`), with a comment recording exactly why. The alternative was
  to duplicate `DatasetVersion` inside F.7, which the brief forbids and which is
  worse engineering.
- **Status:** resolved. **This is the one place in the phase where a test became
  less strict**, so it is stated plainly: the real risk is covered by stricter
  tests that are unchanged — `value_json` appears in no F.7 module, no F.7 file
  names a d20 label or the protected validator, and F.7's own suite now forbids
  importing `src.research` outright.

### 15.6 The persistence layer refused a legal state transition

- **Symptom:** `freeze_research_handoff.py --freeze` raised
  `FrozenArtifactError: canonical_corpus_v2 is already frozen`.
- **Root cause:** `record_corpus_version` refused **any** write to an immutable
  row, including the one legal advance `FROZEN → ELIGIBLE_FOR_RESEARCH` that the
  domain model explicitly permits. The domain and persistence layers disagreed.
- **Fix:** `_guard_immutable()` now allows exactly that transition with an
  unchanged content hash, treats an identical rewrite as a no-op, and refuses
  everything else — a different hash, or a state moving backwards. Three tests
  cover the three cases.
- **Status:** resolved. A real bug, found because the script exercised the
  lifecycle end to end rather than in unit isolation.

### 15.7 The recovered corpus has a materially higher invalid rate

- **Symptom:** 141 of 1,056 observations (13.4%) are `invalid`, against 16 of 581
  (2.8%) in v1.
- **Root cause:** `insufficient_price_data`. **July observations are 24.5%
  invalid against August's 2.8%.** Price history is thinner in July (8,866 rows)
  than August (43,344).
- **Fix:** **none, deliberately.** Invalid observations are marked and kept, never
  deleted, exactly as the existing contract requires. Rebuilding the price cache
  was out of scope and the audit showed prices are complete for the daily
  horizons that matter.
- **Status:** open and reported. The recovered region carries **less usable
  evidence per row** than the region that was already there. More rows is not the
  same as more evidence.

### 15.8 Recovery did not improve two things it was hoped might improve

- **`event_time` is still missing for all 1,056 canonical events** — 0 of 581 in
  v1, 0 of 1,056 in v2. No improvement.
- **`independent_source_count` is still 0 for every canonical event.** Source
  independence remains **UNKNOWN**.
- **Root cause:** neither is derivable from the source material the recovery
  replayed. Both are Phase I-F findings about the upstream schema.
- **Fix:** none. Inferring either would be fabricating precision.
- **Status:** open, deferred, and recorded in the F.6B pre-registration's own
  limitations section so the next phase cannot treat multi-source as corroborated.

### 15.9 The recovered database is too large to commit

- **Symptom:** `data/marketlens_corpus_v2.db` is 195.67 MB and `.gitignore` only
  named `data/marketlens.db`, so the new file was not ignored.
- **Root cause:** a new artifact, not covered by an exact-path ignore rule. It
  would have broken GitHub's 100 MB file limit.
- **Fix:** added it to `.gitignore` with a comment explaining the convention, and
  committed its identity, hashes and counts in this report and the handoff instead,
  so the artifact is reproducible from committed scripts without the file being in
  git.
- **Status:** resolved.

### 15.10 Per-stage timings were partly lost

- **Symptom:** the retained log of the full run was tail-truncated, so the
  entity-resolution and target-preparation timings were not captured.
- **Status:** reported honestly in §37 by subtraction rather than guessed.

### 15.11 Things I deliberately did not do

- **I did not recompute the old holdout.** `2026-08-15T01:23:31Z` is still
  snapshot v1's boundary.
- **I did not re-run F.6 against v2.** That would turn an old experiment into a
  new one wearing its name.
- **I did not rebuild the price cache**, because the audit showed prices were not
  part of the break.
- **I did not touch entity-relationship persistence**, an I-F finding that is not
  responsible for the corpus loss.
- **I did not pre-register intraday horizons**, although they now clear every data
  threshold — see §30.

---

## 16. What was deferred

| Deferred | Why |
|---|---|
| **model research (F.6B)** | explicitly out of scope; F.7 freezes the environment, it does not use it |
| **model qualification** | same; no model was fitted against v2 |
| **target calibration** | requires a qualified model |
| **horizon calibration** | same |
| **production wiring** | out of scope; Portfolio / Risk / Execution untouched |
| **intraday horizons** | data-eligible (57 sessions vs a floor of 30) but **deferred on policy**: the only purge available is the 14-day fallback written for d10 |
| **`event_time` recovery** | not derivable from the replayed source material |
| **source independence** | same; remains UNKNOWN |
| **entity relationship persistence** | not responsible for the corpus loss; scope discipline |
| **price cache rebuild** | prices audited and complete for the recovery period |
| **the 1,757 articles with no mention data** | nothing in the row for the resolver to act on |
| **the 7,099 `ENTITY_UNRESOLVED` articles** | the resolver looked and declined; loosening it is forbidden |
| **D20** | **sealed** |

---

## 17. Recovery run

| | |
|---|---|
| **recovery_run_id** | **`rec-a8472b411f4b4073`** |
| pipeline version | `f7.recovery.v1` |
| code commit | `3dbfde7db4d560d20ca0b39be3c5dab401390d75` |
| source snapshot | `research_snapshot_v1` |
| date range | **2026-07-01T22:25:59Z → 2026-08-30T21:09:11Z (59 days)** |
| started / completed | 2026-10-01T17:30:22Z / 2026-10-01T17:33:22Z |
| **status** | **COMPLETE** |
| processed through | 2026-08-30T21:09:11Z |
| **covers full range** | **True** |
| input count | 48,392 articles |
| errors | **0** |
| skipped | 6 articles at extraction |
| duration | **179.81 s** |
| storage impact | new file **195.67 MB**; source unchanged at 186.92 MB |

### Processed counts

| | |
|---|---|
| entity links | 44,597 |
| event reports | 1,541 |
| canonical events | 1,056 |
| research observations | 1,056 |
| research features | 34,397 |
| research labels | 16,116 |

### Provenance written

| Record type | Rows |
|---|---|
| event_report | 1,541 |
| canonical_event | 1,056 |
| research_observation | 1,056 |
| **total** | **3,653** |

### The validation slice, run first

| | |
|---|---|
| run id | `rec-53e5b53612fb478d` |
| slice | 2026-07-01 → 2026-07-04, the **earliest 3 days** of the never-scanned region |
| selection basis | position in the gap. **Not** market returns |
| method | later articles physically deleted from a throwaway file, not filtered |
| result | 617 articles → 543 entity-resolved (88.0%) → 27 reports → 24 canonical events → 24 observations → 320 labels |
| temporal integrity | 75 records checked, **0 violations** |
| source unchanged | corpus **True**, snapshot **True** |
| **status** | **PARTIAL** — correctly, because a slice does not cover its range |
| duration | 39.1 s |

---

## 18. Before / after coverage

```
BEFORE (corpus v1 / snapshot v1)
  earliest article              2026-07-01T22:25:59Z
  earliest entity link          2026-07-30T13:04:26Z
  earliest canonical event      2026-07-30T13:34:32Z
  entity links                  26,400
  canonical events                 581
  research observations            581
  distinct clusters                244
  information span              28 days
  usable research days              15
  walk-forward folds (d1/d3/d5/d10)  0 / 0 / 0 / 0

AFTER (corpus v2 / snapshot v2)
  earliest article              2026-07-01T22:25:59Z   (unchanged)
  earliest entity link          2026-07-01T22:25:59Z   (the corpus start)
  earliest canonical event      2026-07-02T07:00:00Z
  entity links                  44,597                 (+18,197)
  canonical events               1,056                 (+475)
  research observations          1,056                 (+475)
  distinct clusters                301                 (+57)
  information span              59 days                (+31)
  usable research days              46                 (+31)
  walk-forward folds possible    5 / 5 / 4 / 3
```

**More rows is not the claim.** The integrity evidence for calling this better:
the funnel reconciles, 3,653 records pass the temporal contract with 0 violations,
all lineage walks end to end, no resolution rule was loosened (99.6% resolved,
0.0% ambiguous), and the replay is idempotent to an identical hash. Against that,
the invalid share rose from 2.8% to 13.4% (§15.7), and two provenance fields did
not improve at all (§25, §26).

### An unplanned gain at the recent end

v1's canonical events stop at 2026-08-27T20:04Z because its event build ran on
2026-08-28. Articles published 2026-08-28 to 08-30 had never been processed
either. The recovery picked them up: v2 runs to **2026-08-30T14:35Z**. That was
not the target of the phase and is reported because it is part of the difference.

---

## 19. Temporal coverage

| | v1 | **v2** |
|---|---|---|
| earliest information time (canonical) | 2026-07-30T13:34:32Z | **2026-07-02T07:00:00Z** |
| latest information time (canonical) | 2026-08-27T20:04:01Z | **2026-08-30T14:35:00Z** |
| information span | 28 days | **59 days** |
| earliest observation `information_cutoff` | 2026-07-30T13:34:32Z | **2026-07-02T07:00:00Z** |
| latest observation `information_cutoff` | 2026-08-27T20:04:01Z | **2026-08-30T14:35:00Z** |
| distinct observation days | — | **58** |

---

## 20. Versioning

| | Old | New |
|---|---|---|
| corpus version | **`canonical_corpus_v1`** | **`canonical_corpus_v2`** |
| corpus hash | `99777b5971b088a9012fd22e8da0f7228ddeac09b30d93bc8349c31b00a2ca9d` | `4247f543904204c9d60f86b9ab6858e3bd3bd9fcc635e5950c5b60e148cb2ddb` |
| snapshot id | **`research_snapshot_v1`** | **`research_snapshot_v2`** |
| snapshot hash | `1d0010564b85abeaceb93d56a5b14ba72e71eac874f30c69661ff7ada116c2f7` | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| storage | `data/marketlens.db` | `data/marketlens_corpus_v2.db` |
| state | `ELIGIBLE_FOR_RESEARCH` | `ELIGIBLE_FOR_RESEARCH` |
| schema version | `f7.v1` | `f7.v1` |
| recovery run | — | `rec-a8472b411f4b4073` |
| hash verifies now | **True** | **True** |

**Immutability status: both FROZEN, then advanced to ELIGIBLE_FOR_RESEARCH.**
Enforced at two layers — `transition_to()` refuses illegal transitions and
construction refuses a frozen artifact without a hash and a freeze timestamp;
`_guard_immutable()` refuses any persistence write that would change a frozen
row's content hash or move its state backwards. The only legal change after
freezing is the eligibility advance, with the hash unchanged.

Manifests exclude row order, insertion order and every processing timestamp, so a
replay of the same build produces the same hash. Verified: recomputing both
manifests twice gives identical digests.

---

## 21. Old experiment reproducibility

**Proven, not asserted.**

| | |
|---|---|
| `research_observations` in v1 | **581** — unchanged |
| distinct clusters in v1 | **244** — unchanged |
| v1 corpus hash, recomputed after the recovery | `99777b5971b088a9…` — **identical** |
| v1 snapshot hash, recomputed after the recovery | `1d0010564b85abea…` — **identical** |
| F.6's stored evaluation | `sample_size=5, cluster_count=4, effective_sample_size=4` — **unchanged** |
| old holdout boundary | `2026-08-15T01:23:31Z` — **unchanged** |
| old experiment → old snapshot → old corpus | `research_snapshot_v1` → `canonical_corpus_v1` → `data/marketlens.db` |
| **`data/marketlens.db` file mtime** | **2026-09-11 11:21 — unchanged** |

The file modification time is stronger evidence than either hash: the operating
system records that `data/marketlens.db` was **never written to during this
phase**. The hashes prove the content matches; the mtime proves no write was even
attempted.

The registry recording all of this lives in the **new** file, so proving the old
file was not written to required writing nothing to it. Anyone can recompute both
hashes against the original and compare.

`ResearchSnapshot.masquerades_as()` exists so a new snapshot cannot inherit an old
experiment's identity, and a test asserts the two real snapshots fail it against
each other. The v2 database holds **zero** rows in `trained_models`,
`model_evaluations`, `model_baseline_comparisons`, `predictions`, `signals` and
`recommendations`, so no F.6 artifact can be mistaken for an F.6B one.

---

## 22. Cross-version event differences

Matched by **fingerprint**, which identifies the same underlying event whatever id
each build assigned.

| Relation | Count |
|---|---|
| **SAME** | **579** |
| **NEW** | **474** |
| CHANGED | 1 |
| MERGED | 0 |
| SPLIT | 0 |
| REMOVED | 0 |
| **total mappings** | **1,054** |

**579 of v1's 581 canonical events reappear identically. Nothing was removed.**
One is reported as `CHANGED`. The 474 new events are the recovered history.

Event identity is stable across versions because `populate_events.py` derives
`event_id` from `(article_id, event_type)` rather than a UUID — originally for
idempotency, and it pays off here as cross-version comparability.

Where fingerprints do not match, the event is reported as NEW or REMOVED rather
than forced into a correspondence. An improved pipeline is allowed to cluster
differently; pretending otherwise would hide the difference a later phase needs.

---

## 23. Lineage

Walked programmatically in both directions for recovered observations. One
example, verbatim:

```
RESEARCH OBSERVATION obs-d99f48dde1ffef4b    instrument us_and_intl-msft
  information_cutoff      2026-07-02T07:00:00Z      <- information time
  observation_created_at  2026-10-01T17:28:41Z      <- processing time
  <- CANONICAL EVENT ce-486250dd6e313d80  product_launch
       first_reported_at 2026-07-02T07:00:00Z
       fingerprint evt-d14ac23eaaea15157f8b...
     <- EVENT REPORT evt-d635803be840127a
          publication_time 2026-07-02T07:00:00Z     <- information time
          detection_time   2026-10-01T17:28:38Z     <- processing time
          confidence 0.5108
        <- NORMALIZED / RAW ARTICLE c6ee42d3-81aa-4ed0...
             source     Google News: Microsoft
             published  2026-07-02T07:00:00Z        <- information time
             collected  2026-08-04T12:08:26Z        <- processing time
             entities   [('company', 'microsoft')]
  -> FEATURES  30 values, max as_of 2026-07-02T07:00:00Z   (<= cutoff)
  -> LABELS    d1 2026-07-07, d3 2026-07-09, d5 2026-07-13, d10 2026-07-20
               (measured_at only; no value read)
```

**No anonymous recovered rows.** Asserted across the whole corpus:

| | Violations |
|---|---|
| canonical events with no report | **0** |
| event reports with no source article | **0** |
| evidence rows pointing at a missing article | **0** |
| observations with no canonical event | **0** |
| features or labels with no observation | **0** |
| provenance rows with no recovery run id | **0** |

---

## 24. As-of / temporal integrity

| | |
|---|---|
| **records checked** | **3,653** |
| **violations** | **0** |
| information time | `articles.published_at` → `events.publication_time` → `canonical_events.first_reported_at` → `research_observations.information_cutoff` |
| processing time | `articles.collected_at`, `events.detection_time`, `observation_created_at` |
| recovery time | `recovery_provenance.recovered_at` + `recovery_run_id`, **stored separately and never substituted** |

### Assertions, all passing with zero violations

| Check | Result |
|---|---|
| features with `as_of` after their observation's cutoff | **0** |
| labels maturing at or before their cutoff | **0** |
| `information_time >= recovered_at` in provenance | **0** |
| observations whose processing time precedes their information time | **0** |
| evidence `published_at` disagreeing with its article | **0** |
| events whose publication time precedes their source article | **0** |
| `first_reported_at` later than its earliest evidence | **0** |
| canonical events dated after the latest article | **0** |

### Physical truncation

The validation slice **deleted** later articles from the throwaway file rather
than filtering them, because a filter the stage scripts do not have cannot be
trusted while an absent row can. `effective_sample(before=…)` is tested to see
strictly fewer rows and no more clusters at an earlier boundary.

### Protected isolation

`corpus_funnel.py`, `corpus_versioning.py`, `research_snapshot.py` and
`corpus_recovery_models.py` contain **no reference to `value_json`**, so no F.7
module can read a label value at all — protected or otherwise.

---

## 25. Source independence

**NOT IMPROVED. Recovery exposed no evidence that would allow it.**

| | v1 | v2 |
|---|---|---|
| `independent_source_count` min / max / mean | 0 / 0 / 0.000 | **0 / 0 / 0.000** |
| single-source canonical events | 562 of 581 (96.7%) | **1,036 of 1,056 (98.1%)** |
| multi-source | 19 | 19 |
| contradicted | 0 | 1 |

Source independence remains **UNKNOWN** corpus-wide, exactly as Phase I-F found.
Nothing here invented it, and the F.6B pre-registration forbids treating
`multi_source` as independently corroborated.

The single-source *share* rose slightly, which is the honest direction to report:
the recovered July events are if anything less corroborated than the August ones.

---

## 26. Event time

**NOT IMPROVED.**

| | v1 | v2 |
|---|---|---|
| canonical events with a non-null `event_time` | **0 of 581** | **0 of 1,056** |

True event-occurrence time is not derivable from the replayed source material. An
article's publication time says when something was *reported*, not when it
*happened*, and inferring the second from the first would be fabricating
precision. `event_time` stays NULL, as the project's convention requires.

---

## 27. New research snapshot

| | |
|---|---|
| snapshot id | **`research_snapshot_v2`** |
| corpus version | `canonical_corpus_v2` |
| state | **`ELIGIBLE_FOR_RESEARCH`** |
| observation count | **1,056** |
| date range | **2026-07-02T07:00:00Z → 2026-08-30T14:35:00Z** |
| information span | **59 days** |
| instruments | **301** |
| clusters | **301** |
| canonical events | **1,056** |
| features | 34,397 across 33 names |
| labels | 16,116 across 20 names |
| quality | 915 high, **141 invalid** (all `insufficient_price_data`) |
| horizons present | d1, d3, d5, d10, intraday ×4, **d20 (PROTECTED)** |
| **content hash** | **`4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc`** |
| storage | `data/marketlens_corpus_v2.db` (195.67 MB) |

### Why `dataset_version` still reads `v1`

The snapshot's `DatasetVersion` fingerprint is `v1|v1|v1|v1|v1|v1|v1`, and that is
correct rather than stale. **F.7 changed no feature definition, label definition,
event taxonomy, benchmark or preprocessing step.** What changed is the corpus, and
that is carried by `corpus_version_id = canonical_corpus_v2`.

The snapshot's own identity is `research_snapshot_v2`, so it cannot be confused
with v1: the fingerprint
`research_snapshot_v2|canonical_corpus_v2|v1|v1|...` differs on both the snapshot
id and the corpus version, and `masquerades_as()` returns False between the two
real snapshots. The reading is recorded in the handoff under `version_reading` so
it is not left for someone to re-derive.

---

## 28. Effective sample

Recomputed from the snapshot's own clusters. **No stored evaluation was
consulted**, and there is none in the new database to consult.

| Horizon | Raw rows (region) | **Effective clusters** | Floor | Sample readiness |
|---|---|---|---|---|
| **d1** | 737 | **227** | 30 | **READY** — 7.6× |
| **d3** | 735 | **227** | 30 | **READY** — 7.6× |
| **d5** | 735 | **227** | 30 | **READY** — 7.6× |
| **d10** | 633 | **211** | 30 | **READY** — 7.0× |
| intraday ×4 | 738 | 227 | 30 | ready on sample; deferred on policy |
| **d20** | — | — | — | **PROTECTED** |

Whole-snapshot clusters: **301** (v1: 244).

### The stale-value trap, disarmed

| | |
|---|---|
| F.6's stored `effective_sample_size` | **4** — one five-row test window, 2026-08-29 |
| rows in `model_evaluations` in the v2 database | **0** |
| `model_evaluations` referenced in `research_snapshot.py` code | **no** (asserted against the parsed AST) |
| `effective_sample_size` referenced in its code | **no** |

Stored historical evaluation sample and current snapshot capacity are kept
explicitly distinct: the first lives only in v1 as evidence about v1, and the
second is computed on demand from clusters.

---

## 29. Temporal research window

| Horizon | Research days | Purge | Embargo | Test | Step | 1 fold needs | 3 folds need | **Max folds** |
|---|---|---|---|---|---|---|---|---|
| **d1** | 46 | 3 | 6 | 7 | 7 | 16 | 30 | **5** |
| **d3** | 46 | 5 | 6 | 7 | 7 | 18 | 32 | **5** |
| **d5** | 46 | 7 | 6 | 7 | 7 | 20 | 34 | **4** |
| **d10** | 46 | 14 | 6 | 7 | 7 | 27 | 41 | **3** |
| intraday ×4 | 46 | 14 | 6 | 7 | 7 | 27 | 41 | 3 |

| | |
|---|---|
| earliest usable observation | 2026-07-02T07:00:00Z |
| latest pre-holdout observation | before 2026-08-18T00:00:00Z |
| **research-region length** | **46 days** |
| adequacy verdict | **ADEQUATE** |
| horizons not viable | **none** |

### Against F.6, which is the whole point

| Horizon | F.6 region | F.6 folds | F.6 shortfall | **F.7 region** | **F.7 folds** |
|---|---|---|---|---|---|
| d1 | 15 days | **0** | 15 days | **46 days** | **5** |
| d3 | 15 days | **0** | 17 days | **46 days** | **5** |
| d5 | 15 days | **0** | 19 days | **46 days** | **4** |
| d10 | 15 days | **0** | 26 days | **46 days** | **3** |

F.6's shortfall arithmetic is preserved in a test that reproduces all four
figures exactly, so the two phases cannot drift into describing different
contracts.

**The old frozen holdout was not changed.** `2026-08-15T01:23:31Z` remains
snapshot v1's boundary.

---

## 30. New pre-registration

| | |
|---|---|
| artifact | **`docs/PHASE_25_9I_F6B_PREREGISTRATION.md`** |
| **sha256** | **`9150b988993dcac3789b99140eaec782df60e816c8fa77fd25be0d4f18087625`** |
| dataset | `research_snapshot_v2` / `canonical_corpus_v2` |
| split policy | expanding day-granular walk-forward inside the research region |
| purge | calendar span per label: d1→3, d3→5, d5→7, d10→14 days |
| embargo | 6 days |
| test window / step | 7 / 7 days |
| minimum valid folds | **3** |
| holdout policy | final 20% of the information span, floored to the UTC day; not opened unless a candidate survives the research region |
| model budget | **16 cells** (4 candidates × 4 horizons), fixed |
| baseline policy | `ModelingEngine.train_and_evaluate`'s mandatory baselines, unchanged |
| metrics | primary metric per task; Brier vs base rate for probabilistic candidates |
| qualification gate | `src.domain.model_models.ModelEvaluation.is_deployable` |
| handoff | `docs/PHASE_25_9I_F6B_HANDOFF.json` |

**Frozen BEFORE F.6B: confirmed.** No model has been fitted against snapshot v2.
The v2 database holds zero trained models, zero evaluations and zero predictions,
and tests assert both that and the absence of any call to `train_and_evaluate`,
`evaluate_fold`, `ModelingEngine` or `promote_model` in the F.7 scripts.

### Intraday: eligible on data, deferred on policy

The intraday horizons now have **57 distinct sessions** against a floor of 30 —
F.6 deferred them at 29 — and clear every data threshold. They are still deferred,
and the reason is worth stating because it is the kind of decision that is easy to
get wrong at the end of a long phase: **the only purge horizon available to them
is the 14-day fallback written for d10.** Fourteen calendar days of purge around a
five-minute forward window is a placeholder, not a policy.

An intraday purge must be pre-registered on its own evidence, in a document of its
own, and must not be chosen because it produces more folds. That is exactly why it
was not chosen here, in a document whose other numbers are already known.

### The budget did not grow

16 cells, the same as F.6, with twice the corpus. A larger dataset is not a licence
for a wider search.

---

## 31. New holdout

| | |
|---|---|
| **boundary** | **`2026-08-18T00:00:00Z`** |
| rule | the final **20%** of the snapshot's information-time span, floored to the start of the UTC day |
| provenance of the rule | **Phase 25.9's fraction, carried over unchanged** — it predates this dataset, so it cannot have been chosen to suit it |
| research region | 2026-07-02T07:00Z → 2026-08-18T00:00Z, **46 days** |
| holdout region | 2026-08-18T00:00Z → 2026-08-30T14:35Z, **12 days (21.26%)** |
| decided at | 2026-10-01, before any candidate was fitted |
| `observed_model_outcome` | **False** |
| holdout opened | **NO** |

Legitimately frozen: the boundary is a pure function of the information range,
computed by `deterministic_holdout()` and tested to be deterministic across calls.
`HoldoutDecision` refuses to construct when `observed_model_outcome` is set and
refuses a boundary with no stated rule, so a boundary chosen after a result cannot
be frozen as a holdout at all.

**No model outcome is shown here, because no new model was run.**

---

## 32. F.6B readiness

**YES.**

| Requirement | State |
|---|---|
| new dataset validated | **YES** — funnel reconciles, 3,653 records, 0 temporal violations, hashes verify |
| enough temporal history | **YES** — 46-day research region against a 16–27 day minimum |
| at least one valid walk-forward fold | **YES** — 5 / 5 / 4 / 3 for d1 / d3 / d5 / d10 |
| pre-registration frozen | **YES** — sha256 `9150b988993dcac3…` |
| holdout frozen | **YES** — `2026-08-18T00:00:00Z`, before any outcome |
| D20 isolated | **YES** — §33 |
| no unresolved integrity blocker | **YES** — the open items in §15.7, §25 and §26 are documented limitations, not integrity failures, and each is written into the F.6B pre-registration's own limitations section |

---

## 33. D20 status

| | |
|---|---|
| executed? | **NO** |
| inspected? | **NO** |
| inferred? | **NO** |
| consumed? | **NO** |
| used to choose recovered records? | **NO** |
| used to choose the holdout? | **NO** |
| used to assess recovery quality? | **NO** |
| hypothesis changed? | **NO** |
| anchor-v2 changed? | **NO** |
| protected window changed? | **NO** |

Enforced structurally:

- `research_snapshot.effective_sample()` **raises** if asked about `d20`.
- `ResearchHandoff` **refuses to construct** unless `d20_excluded` is true.
- `horizon_ids()` excludes it by default; the recovery reports it as `PROTECTED`
  with no counts.
- Boundary tests parse every F.7 module and script and assert the absence of
  `validate_d20_reversal`, `anchor_v2`, `build_anchor_v2_labels`,
  `d20.abnormal_return`, `d20.raw_return`, `check_d20_readiness` and `return_60d`,
  and that no F.7 file restates the frozen hypothesis in prose either.
- No F.7 file imports `src.research` or `protected_ledger`.
- `value_json` appears in no F.7 module, so none can read a label value.

### One caveat, stated rather than buried

`build_research_observations.py` is the canonical label builder and it writes
**all** horizons' labels, d20 among them, in the new file as it does in the old.
F.7 reused it **unchanged**, because forking it would create the second pipeline
the brief forbids. What F.7 does is quarantine the horizon at every layer above
the builder: no F.7 code reads a d20 value, no report states its counts, and the
handoff cannot carry it.

The protected **experiment** — its hypothesis, anchor-v2 semantics, window,
validator and its results in v1 — is untouched, and v1's hashes prove it.

---

## 34. Production behaviour

| | |
|---|---|
| Portfolio changed? | **NO** |
| Risk changed? | **NO** |
| OrderIntent changed? | **NO** |
| Execution changed? | **NO** |
| Capture changed? | **NO** |
| IBKR changed? | **NO** |
| Subscriptions changed? | **NO** |
| Production model changed? | **NO** |

Two files were modified and neither is a production module: `.gitignore`, and one
test file (§15.5). `data/marketlens.db` was never opened for writing.

Boundary tests assert no F.7 file imports `src.execution`, `src.brokers`,
`src.paper`, `src.trading`, `src.ibkr`, `ib_insync`, `ibapi`, `src.portfolio`,
`src.risk`, `src.capture`, `src.signals` or `src.impact`; names no order or broker
symbol in code; writes to no order or execution table; and defines no feature flag.

---

## 35. Broker safety

```
broker writes attempted:  0
broker writes succeeded:  0
paper orders:             0
live orders:              0
```

No executable `OrderIntent` was created. No IBKR connection was opened. No
market-data subscription was purchased or changed. No market-data request was
made, so no snapshot billing was incurred.

---

## 36. Test results

### Full suite

| | Baseline `1be725d` | **Final** |
|---|---|---|
| Tests | 5,119 | **5,275** |
| Pass | 5,118 | **5,274** |
| Fail | 0 | **0** |
| Errors | 0 | **0** |
| Skipped | 1 | **1** |
| Runtime | 488.078 s | **563.048 s** |

```
PYTHONPATH=src python -m unittest discover -s tests -t . -b
Ran 5275 tests in 563.048s
OK (skipped=1)
```

**+156 tests, exactly the three new files. Zero regressions.**

### Targeted and requested suites

| Suite | Tests | Pass | Fail | Err | Runtime |
|---|---|---|---|---|---|
| **F.7 — `test_corpus_recovery`** | **71** | 71 | 0 | 0 | 42.7 s |
| **F.7 — `test_research_snapshot_integrity`** | **55** | 55 | 0 | 0 | 0.6 s |
| **F.7 — `test_recovery_boundary`** | **30** | 30 | 0 | 0 | 11.7 s |
| adaptive (F.7 + F.6 + F.5 + I-F + I-E + I-D + I-C + I-B) | **931** | 931 | 0 | 0 | 63.1 s |
| news normalization | 107 | 107 | 0 | 0 | 0.09 s |
| entities | 84 | 84 | 0 | 0 | 0.15 s |
| events (extraction) | 81 | 81 | 0 | 0 | 0.08 s |
| fusion | 47 | 47 | 0 | 0 | 0.03 s |
| research dataset / labels / research integrity | 104 | 104 | 0 | 0 | 12.2 s |
| experiments (Experiment Engine) | 83 | 83 | 0 | 0 | 6.4 s |
| modeling | 142 | 142 | 0 | 0 | 2.8 s |
| signals | 129 | 129 | 0 | 0 | 3.3 s |
| portfolio | 273 | 273 | 0 | 0 | 0.8 s |
| trading (readiness, live safety, Risk) | 224 | 224 | 0 | 0 | 5.2 s |
| capture / session | 126 | 126 | 0 | 0 | 276.3 s |

Ingestion has no suite of its own; it is covered by the news, entity and event
suites above.

---

## 37. Performance / storage

| Operation | Measured |
|---|---|
| historical funnel scan + full audit (v1) | **46.4 s** |
| full audit (v2) | **36.1 s** |
| recovery plan (read-only) | < 2 s |
| entity-resolution dry run over 48,392 articles | ~60 s |
| **validation slice, end to end** | **39.1 s** |
| **full recovery, end to end** | **179.8 s** |
| &nbsp;&nbsp;event extraction | 50.4 s |
| &nbsp;&nbsp;fusion | 3.9 s |
| &nbsp;&nbsp;event studies | 65.4 s |
| &nbsp;&nbsp;research observations | 5.9 s |
| &nbsp;&nbsp;features | 34.1 s |
| &nbsp;&nbsp;target preparation + entity resolution + verification | ~20 s (by subtraction — see §15.10) |
| manifest / hash generation | < 1 s for 1,056 events and 1,056 observations |
| effective-sample computation | < 0.1 s per horizon |
| handoff freeze | < 2 s |

### Storage

| | |
|---|---|
| source database, before | **186.92 MB** |
| source database, after | **186.92 MB — unchanged** |
| new database | **195.67 MB** |
| peak during preparation | ~196 MB (copy) before VACUUM |
| net disk growth | **+195.67 MB**, in a gitignored Release asset |
| rows cleared during preparation | **93,183** (70,392 derived + 22,791 v1 experiment) |

---

## 38. Scope-creep review

| | |
|---|---|
| Unrelated files changed | **none** |
| Modeling changes | **none** |
| Model thresholds | **none changed.** `MIN_EFFECTIVE_SAMPLE` (30) and `MIN_VALID_FOLDS` (3) are aliased from the canonical constants; a test forbids a numeric literal being assigned to either name in F.7 code |
| Qualification gates | **none changed.** `ModelEvaluation.is_deployable` is referenced, never redefined |
| Execution | **untouched** |
| Market-data behaviour | **untouched.** 0 requests, 0 subscription changes |
| Protected research | **untouched** |
| External dependencies | **none added.** Standard library plus existing project modules |
| Paid external calls | **0** |
| Production modules modified | **0** |
| Files modified in total | **2** — `.gitignore` and one test file, both explained in §4 and §15 |

One test became less strict and it is called out in §15.5 rather than left for a
reader to find.

---

## 39. Known limitations

Specific only.

1. **July observations are 24.5% invalid** for `insufficient_price_data`, against
   2.8% in August. 141 of 1,056 overall. The recovered region carries less usable
   evidence per row than the region that already existed.
2. **`event_time` is missing for all 1,056 canonical events**, exactly as in v1.
   Event-occurrence timing was not improved and is not inferred.
3. **`independent_source_count` is 0 for every canonical event.** Source
   independence remains UNKNOWN; 1,036 of 1,056 events are single-source.
4. **A cluster is an instrument**, so two separate events at the same issuer count
   as one cluster. Conservative, and coarse.
5. **The new holdout is 12 days.** It satisfies the inherited rule, but a single
   holdout evaluation on 12 days of data is weak evidence even when it passes.
6. **1,757 pre-cutoff articles have no mention data** and are not recoverable by
   replaying entity resolution — there is nothing in the row to resolve.
7. **7,099 articles are `ENTITY_UNRESOLVED`** and were deliberately not recovered:
   the resolver looked and declined, and loosening it is forbidden.
8. **Intraday horizons are deferred on policy, not on data** (§30).
9. **The recovered database is not in git** (195.67 MB). Its identity, hashes and
   counts are committed; the artifact itself must be rebuilt or distributed as a
   Release asset.
10. **`--since` still exists** on `backfill_article_entities.py` and
    `populate_events.py`. F.7 does not pass it and a test prevents the recovery
    from doing so, but the flag remains available to a human running the scripts
    directly.
11. **Per-stage timings for entity resolution and target preparation were lost**
    from the retained log and are reported by subtraction (§15.10).
12. **One skipped test** in the full suite, unchanged from baseline.

---

## 40. Final verdict

> ## PHASE 25.9I-F.7 IMPLEMENTATION = COMPLETE
>
> ## HISTORICAL CORPUS RECOVERY = RECOVERED AND VALIDATED
>
> ## F.6B MULTI-HORIZON RESEARCH READINESS = YES

**Evidence.**

**The root cause was proven from records, not inferred.**
`scripts/backfill_article_entities.py` was run with a `--since` publication floor
at approximately **2026-07-30T13:04:26Z**, so **17,789 articles published before
it were never scanned**. `populate_events.py` selects only entity-linked articles,
so no event, canonical event or research observation could exist for the first
month of the corpus. The decisive measurement is a single collection run on
2026-08-23 that produced 14,696 entity links for articles published after that
instant and **0** for the 9,767 published before it — same run, same code, same
sources. Entity resolution loses 23.2% of what it is given on one side of that
instant and **100%** on the other.

**The recovery replayed the existing stages without that floor.** Entity links
26,400 → **44,597**; canonical events 581 → **1,056**; observations 581 →
**1,056**; clusters 244 → **301**; information span 28 → **59 days**; research
region 15 → **46 days**. The first material break moved from entity resolution to
event extraction, which is what a working pipeline looks like.

**It is validated, not merely larger.** The funnel reconciles with 0 of 48,392
unclassified. **3,653 recovered records passed the temporal-integrity contract
with 0 violations**, and a single violation would have marked the run FAILED. All
lineage walks end to end with no anonymous rows. No resolution rule was loosened —
99.6% resolved, 0.0% ambiguous. The replay is idempotent to an identical manifest
hash: all six stages run twice produced **0** new rows.

**The old experiment is intact.** `data/marketlens.db` was opened `mode=ro`
throughout, and its file modification time is still **2026-09-11 11:21** — the
operating system records that it was never written to at all. Its corpus hash
`99777b5971b088a9…` and snapshot hash `1d0010564b85abea…` are identical before and
after, recomputed from the untouched file. F.6's stored evaluation still reads `sample_size=5, cluster_count=4,
effective_sample_size=4`, and its holdout is still `2026-08-15T01:23:31Z`. 579 of
581 canonical events reappear in v2 identically; nothing was removed. **F.6's
result — NONE QUALIFIED / INSUFFICIENT DATA — stands exactly as recorded and is
not reinterpreted.**

**The new environment is frozen before any research.** Holdout
`2026-08-18T00:00:00Z`, from Phase 25.9's 20% fraction applied mechanically —
inherited precisely so it could not be chosen to suit this dataset. Adequacy
**ADEQUATE**: d1, d3, d5 and d10 form **5, 5, 4 and 3** folds where F.6 formed
**0, 0, 0 and 0**. Pre-registration sha256 `9150b988993dcac3…`, budget fixed at
**16 cells** — the same as F.6, with twice the corpus.

**F.6B was not run.** Zero trained models, zero evaluations, zero predictions in
the new database, and no F.7 script can fit one.

**What recovery did not fix is stated plainly**: the invalid share rose from 2.8%
to **13.4%** (24.5% in July), `event_time` is still missing for all 1,056 events,
and `independent_source_count` is still 0 everywhere. More rows is not more
evidence, and those three numbers are why that sentence is in the report rather
than only in the brief.

**D20 remains sealed** — not executed, inspected, inferred or consumed, and not
used to choose which records to recover or where to place the holdout. Broker
writes, paper orders and live orders: **0 / 0 / 0**. Portfolio, Risk, OrderIntent,
Execution, capture, IBKR and subscriptions: **unchanged**. **5,275 tests pass,
OK (skipped=1), against a 5,119-test baseline.**

**The recovered dataset makes the question askable. It says nothing whatever about
the answer.**
