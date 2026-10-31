# Phase 25.9D — Research / Experiment Infrastructure Audit & Hardening Report

**Written** 2026-09-16 · **Base commit** `9144395` · **Protected test** NOT EXECUTED · **Protected result** UNSEEN

---

## A. Executive summary

**The research system could report stale, protected or inflated evidence as current. It no longer can.**

Every finding was reproduced with a probe before anything was fixed:

| # | What the probe showed | Severity |
|---|---|---|
| F1 | A revised outcome turned the true effect from **+0.236 into −0.097**. The cache still served **+0.236**. Cutoff and fingerprint were identical. | HIGH |
| F4 | A challenger stamped with cutoff C was measured on **360 rows, 60 of them after C**. | HIGH |
| F6 | An autoresearch cycle read **400 rows inside a declared protected window** and concluded "supported, promising". | HIGH |
| F5 | A protected window declared after a first run did not stop the cached result being served. | HIGH |

Also fixed: a queue race that could run a finished item again (F2), inflated multiple-testing counts (F3), a crash retry counted as a second look (F8), window bounds compared as strings (F7), and the leakage guard missing respelled field names (F9).

**What this did not affect:**

- **Production.** The production snapshot has 0 runs, 0 results and 0 queue items. Every defect was latent, and no stored evidence was corrupted.
- **D20.** The research packages cannot read labels or the ledger. D20 stays unseen, unexecuted and unconsumed.

---

## B. Baseline

| | |
|---|---|
| branch / commit | `ibkr-paper-validation-fixes` / `9144395`, clean |
| Python | 3.12.10 |
| production snapshot | 276,164,608 bytes, sha256 recorded and re-verified after every step |
| production research tables | 6 draft experiments, 0 runs, 0 results, 0 queue, 0 windows, no challenger tables |
| `trading_experiences` | 11,641 rows, 3,663 without `available_at`, written in one batch |
| protected ledger | intact, D20 `NOT_CONSUMED` |

---

## C. Architecture as audited

```
trading_experiences ──load_cohort──► experiments.engine.run ──► experiment_runs / experiment_results
        ▲                                   ▲        ▲
        │                        autoresearch.cycle   challengers.evaluation (own cache, same loader)
        │                                   │
   memory build                  queue (claim / reclaim), governance (leakage, windows, snooping ledger)
```

D20 is separate: `research_observations` / `research_features` / `research_labels` (anchor-v2) → `check_d20_readiness.py` / `validate_d20_reversal.py` → `research/protected_tests/ledger.jsonl`. No research package imports or names any of these; asserted by test (§AC).

---

## D. Identity model

| Identity | What it covers | Changes when |
|---|---|---|
| `Experiment.fingerprint` | hypothesis, both arms, dataset **definition**, protocol, criteria | the question changes |
| `DatasetSnapshot.snapshot_id` | `as_of`, `data_cutoff`, universe, filters, versions | the dataset definition changes |
| `current_data_cutoff` | `MAX(available_at)` | the record grows past its frontier |
| **`cohort_digest`** (new) | the loaded rows, all 25 columns the engine reads | **any row the cohort can see changes** |
| `run_id` | one attempt (uuid) | every attempt |
| `seed` | bootstrap randomness | by request |

The gap was between row 3 and the rows themselves. Nothing identified the content.

---

## E. F1 — stale cache behind the frontier (HIGH)

**The defect.** The cache key was `(fingerprint, seed)`. The Phase 23.5 fix put `data_cutoff` into the fingerprint, which only catches a record that grows **past** its newest timestamp.

**Reproduced.** 240 experiences, one run, then each mutation below, then a second run:

| Mutation | Cutoff same | Fingerprint same | Before fix | After fix |
|---|---|---|---|---|
| 60 late rows dated inside the span | yes | yes | cache hit, **+0.2361** on 240 rows (true +0.2333, 300 rows) | recomputed, +0.2333 |
| 60 rows reclassified out of `validated` | yes | yes | cache hit, **+0.2361** (true +0.1692, 180 rows) | recomputed, +0.1692 |
| `direction_result` revised | yes | yes | cache hit, **+0.2361** (true **−0.0972**) | recomputed, −0.0972 |

**Fix.**
1. `engine.cohort_digest(rows)` hashes the loaded cohort.
2. The run loads the cohort **before** it considers reuse.
3. The cache key is now `(fingerprint, seed, cohort_digest)`.
4. The challenger cache uses the same key.
5. The new `cohort_digest` column is added in place; old rows get `''`, which never matches, so a pre-fix run cannot be a cache source.

**Cost.** One cohort read per run. The read is capped by `max_rows` and was already paid by every non-cached run. On the real working copy, 7,596 rows took under a second.

---

## F. F4 — the recorded cutoff was a label, not a bound (HIGH)

`load_cohort` applied `as_of` but ignored `data_cutoff`. A challenger's fingerprint claimed cutoff C while it read past C. **Reproduced:** 300 → 360 rows, 60 beyond the cutoff. **Fix:** `available_at <= data_cutoff` whenever it is set. Autoresearch sets it to the current frontier, so today's cohorts are unchanged. Re-running a stored experiment now reproduces its own dataset.

---

## G. F5 / F6 / F7 — protected windows

| | Before | After |
|---|---|---|
| experiments API | no check | refused |
| autoresearch cycle | **no check**: 400 rows, "supported, promising" | refused; no result, conclusion, candidate or snooping use |
| challenger, fresh | test half checked | whole cohort checked |
| challenger, **cached** | **served** | refused before the cache |
| `2026-08-27T10:00+00:00` vs window ending `2026-08-27` | **allowed** (string order) | refused |
| `Z` / `+00:00` / offsets | compared as text | compared as UTC instants |
| unreadable bound | compared as text | **fail closed** |

The check sits in `engine.run`, the one path every experiment takes. It covers the whole cohort, because training on a reserved region is tuning against it. The guard reads the windows table without creating it, so a database with no research tables is left untouched (tested).

**Behaviour change, stated plainly:** once any protected window is declared, an unbounded autoresearch cohort overlaps it and is refused, until the researcher sets `as_of` before the window. That is fail-closed by design. Production declares no windows, so nothing changes there today.

---

## H. F2 — queue concurrency and stale reclaim (MEDIUM)

| Case | Result |
|---|---|
| two connections claim one item | exactly one wins (already correct) |
| worker completes between reclaim's SELECT and UPDATE | **before:** requeued, would run twice · **after:** stays `completed` |
| genuinely abandoned item | still reclaimed |

**Fix:** the reclaim UPDATE now also requires `state='running' AND started_at IS <value read>`, and only rows it actually moved are reported. There is no heartbeat, but `STALE_AFTER_SECONDS` = 3600 against runtime caps of 300 s per experiment and 900 s per cycle. A healthy run cannot be reclaimed.

---

## I. F3 / F8 — retry is not new evidence (MEDIUM)

**F3.** `family_statistics` counted stored result rows, and a cache hit saves one. **Reproduced:** one measurement read four times reported comparisons 2, 3, 4, 5. **Fixed:** a comparison is a distinct `(fingerprint, seed, cohort_digest)` from a non-cache run. Legacy rows count once each, so unknown content is never merged away. A new seed still counts, because seeds can be shopped. The challenger `family_run_count` got the same fix.

**F8.** The cycle recorded a snooping-ledger use before knowing whether anything was measured. **Reproduced:** crash after the result, reclaim, retry → **2 window uses for 1 measurement**. That pushes the reuse warning toward blocking a legitimate result. **Fixed:** a use is recorded only for a fresh measurement: not for a refused run, not for a cache hit.

The crash-retry probe after the fix: 1 conclusion, ≤1 candidate, 1 window use, and the retry is a labelled cache hit.

Both errors pointed the conservative way, over-counting rather than under-counting. That is why they are MEDIUM and not HIGH.

---

## J. Crash and partial-write recovery

| Crash point | Outcome | Handling |
|---|---|---|
| after `claim`, before the experiment | queue `running` | reclaimed after 1 h |
| inside `engine.run`, non-research exception | queue `rejected` with reason; **experiment left `running`, no run row** | new integrity check `experiments_stranded_running` |
| after `save_run`, before the conclusion | queue `running`; run + result saved | reclaimed → cache hit → one conclusion (tested) |
| between run and result insert | impossible: both in one commit | — |
| conclusion / candidate twice | ids derived from hypothesis + experiment, `INSERT OR REPLACE` | idempotent |

A stranded `running` experiment is detected, not auto-repaired (LOW). It blocks nothing: a re-run is allowed.

---

## K. Lineage

The existing checks cover conclusion→hypothesis, hypothesis→question, question→observation, queue→hypothesis, candidate→conclusion, result→run, run→experiment and the challenger chain. Added: cache hit → a source that exists and read the **same** cohort, seed and fingerprint; and completed queue item → an existing experiment. All zero on production and on the rehearsal copy.

---

## L. Point-in-time and leakage

| Check | Result |
|---|---|
| cohort filter on `available_at` (knowable time), ordered chronologically | PASS (existing tests) |
| `data_cutoff` enforced as bound | **fixed (F4)** |
| rows with `available_at < information_cutoff` in production | 0 |
| evaluators can filter only on registered decision-time parameters | PASS: structural, the registry cannot express an outcome filter |
| leakage guard: `Primary_Error`, `␠actual_return`, `DIRECTION_RESULT`, `outcome.primary_error` | **before: all passed** · after: refused (F9, LOW, defence in depth) |
| `actual_direction`, `time_to_mfe_seconds`, `attribution_*`, `evidence_count` | were unclassified → now outcome-derived |
| main chronological split has no purge/embargo for horizon overlap (walk-forward has) | LOW, documented, unchanged |

---

## M. Research memory point-in-time

Observations mine memory patterns built from the **whole** record, including the half they are later tested on. This is disclosed inside every mined hypothesis's mechanism text ("found in the same record the test will use"), and the reuse ledger counts it. **LOW, disclosed, unchanged.** Closing it properly means building memory as-of the split, which is a design change beyond a bounded fix.

---

## N. Multiple testing and data snooping

- Family comparison counting fixed (F3). Snooping ledger fixed (F8).
- **Cross-family duplicates (LOW, pre-existing, detected).** Production holds six never-run draft proposals. Four of them share one comparison (`signal_strength_threshold` 0.5 vs all) across four different families. `duplicate_comparisons = 2` on production. The per-family count cannot see across families, but the integrity command does. They have never been run, so there is no evidence to deflate. Left as a finding for a human to prune.
- Experiment status reflects the latest run. A new-seed run can move `passed` ↔ `inconclusive`; every seed is now a counted comparison. LOW.

---

## O. Negative knowledge and conflicting evidence

A failed or refused run is recorded as a refusal, never as a finding (existing test). A rejected hypothesis is kept. A second cycle over the unchanged real record skipped 6 duplicates and formed 1 new hypothesis (§AA). `_conflicting_conclusions` is unchanged. PASS.

---

## P. Candidate governance and autonomous promotion

| Check | Result |
|---|---|
| candidates require review | 0 violations (prod, rehearsal) |
| promoted candidates / challengers in a production state | 0 |
| a promotion path from research code | none: existing source tests plus the new static scanner |
| rehearsal on real data | 4 conclusions, 0 promising, **0 candidates** |

**Autonomous promotion: BLOCKED.**

---

## Q. Transitive write boundary — measured

The Phase 23 boundary test counts **rows**, so an `UPDATE` to a trading table is invisible to it. Proven: a `+1` to one `actual_return` leaves counts identical. A content-hash test now sits beside it, with that negative control.

**Real-data rehearsal** (§AA): 71 tables hashed row by row, before and after.

| | |
|---|---|
| tables moved | 19 |
| research-scope | **19** |
| **outside research scope** | **0** |
| negative control: one cell `+1e-9` in `trading_experiences` | **detected** |

Static scan: every table written by `src/experiments`, `src/autoresearch` and `src/challengers` is a research table (15 files, 21 tables).

---

## R. Integrity command

`scripts/audit_research_integrity.py`

- opens the database `mode=ro`, copies it to memory, and audits the copy; the file's sha256 was re-verified after every run
- runs the three existing subsystem checks, five new checks, the static write scanner and the ledger check
- `--negative-controls`: **10/10**. A clean database reads clean; each injected defect fires **only its own** check; the scanner must find three known real writes and an injected `UPDATE trained_models`; a consumed D20 and a tampered chain are each detected

**The controls earned their place during this phase.** A lost regex escape made the write scanner match nothing, and it reported a clean boundary. The "finds the known research writes" control caught it.

| Database | Result |
|---|---|
| production snapshot | 1 finding: `duplicate_comparisons = 2` (§N) |
| rehearsal copy | the same 1 finding, nothing new |

---

## S. D20 isolation

| | |
|---|---|
| research packages naming `research_labels`, `anchor-v2`, `validate_d20`, `check_d20`, `protected_ledger`, `protected_tests` | none (tested) |
| `validate_d20_reversal.py` executed | **no** |
| readiness checker run (states only) | NOT READY, 207 pending, 0 resolvable |
| ledger md5 before / after | `7113fa4e…` / `7113fa4e…` |
| D20, anchor, ledger, readiness code changed since `9144395` | none |

The autoresearch evaluation window on the real copy (2026-08-26..09-04) shares calendar days with the D20 window. It reads `trading_experiences` signal outcomes, not the D20 feature or its labels, so it cannot compute or approximate the protected statistic. D20's window was deliberately **not** declared in autoresearch governance: that would refuse all unbounded research on unrelated data, to protect data research cannot reach.

---

## T–Z. Experiment-integrity matrix

| Property | Experiments | Autoresearch | Challengers | Evidence |
|---|---|---|---|---|
| stale cache on revised content | **FIXED** | via engine | **FIXED** | §E, tests |
| cutoff enforced | **FIXED** | via engine | **FIXED** | §F |
| protected window, fresh | **FIXED** | **FIXED** | PASS→whole cohort | §G |
| protected window, cached | **FIXED** | **FIXED** | **FIXED** | §G |
| window bound formats | **FIXED** | **FIXED** | **FIXED** | §G |
| single execution (claim) | — | PASS | existing | §H |
| stale reclaim race | — | **FIXED** | — | §H |
| comparisons exclude reuse | **FIXED** | via engine | **FIXED** | §I |
| retry ≠ snooping use | — | **FIXED** | unchanged (records on fresh only) | §I |
| crash recovery | detected | PASS | PASS | §J |
| lineage | PASS | PASS | PASS | §K |
| point-in-time | **FIXED** | **FIXED** | **FIXED** | §L |
| leakage guard | structural PASS | **HARDENED** | structural PASS | §L |
| memory PIT | — | LOW, disclosed | — | §M |
| autonomous promotion | none | BLOCKED | BLOCKED | §P |
| write boundary (measured) | PASS | PASS | PASS | §Q |
| D20 reachable | no | no | no | §S |

### Version-field classification (§86)

| Field | Class | In fingerprint | In cache key |
|---|---|---|---|
| hypothesis, arms, criteria, protocol | definition | yes | via fingerprint |
| `as_of`, `data_cutoff`, filters, universe | dataset definition | yes | via fingerprint |
| `dataset_version`, `feature_version`, `label_version` | declared versions | yes | via fingerprint |
| `code_version` | provenance | no | no (recorded per run) |
| `seed` | randomness | no | **yes** |
| `cohort_digest` | **content** | no | **yes** |
| `run_id`, timestamps, environment | attempt | no | no |

`code_version` is outside the key by existing design. A code change that alters evaluator behaviour on identical rows would still cache-hit. **LOW, documented.** Evaluator changes are rare, and a forced `--no-cache` exists.

### Research-cache cases (§87)

| Case | Required | Result | Test |
|---|---|---|---|
| A unchanged exact dataset | may hit | **hits**, labelled with source run | `test_case_a_…` |
| B new eligible records | must not serve old | **misses** (behind or past the frontier) | `test_case_b_…` + Phase 23.5 test |
| C revised / reclassified contents | must not serve old | **misses**, reports the true effect | `test_case_c_…` ×2, challenger ×1 |
| D changed definition | new identity | **new fingerprint, misses** | `test_case_d_…` |
| edit to a row outside the cohort | should still hit | **hits** | `test_an_edit_outside…` |

---

## AA. Real-data rehearsal

Working copy `scratchpad/25_9d/rehearsal-wc.db`, copied from the production snapshot. The path guard refuses `data/marketlens.db`, anything in the repo's `data/`, and anything outside the scratchpad.

| Step | Exit | Time |
|---|---|---|
| `run_research.py --questions --apply` | 0 | 0.9 s |
| `run_research.py --cycle --apply --max-experiments 3` | 0 | 5.8 s: 4 hypotheses, 3 run |
| same again, unchanged record | 0 | 3.8 s: 6 duplicates skipped, 1 run |
| `run_research.py --check` | 0 | 0.6 s |
| `run_challenger.py --candidates` | 0 | 0.7 s: none to challenge |
| `audit_research_integrity.py` | 1 | 1.9 s: the pre-existing §N finding |

Every run on 7,596 rows carried a 35-character digest. Re-running a completed experiment gave a **cache hit with the identical digest**. A forced rerun gave the identical effect (−0.01046) and **1 comparison**. The four conclusions were 2 `insufficient_data` and 2 `inconclusive`: none promising, no candidates. Table diff: §Q. Production snapshot sha256: **OK** after all steps.

---

## AB. Findings

| ID | Severity | Finding | Evidence | Reproduced? | Fixed? | Tests | Remaining Risk |
|---|---|---|---|---|---|---|---|
| F1 | HIGH | Cache keyed on definition, not content: late, revised or reclassified rows behind the frontier served stale results (experiments and challengers) | +0.236 served, true −0.097 | Yes | Yes | 8 | `code_version` outside key (LOW) |
| F4 | HIGH | `data_cutoff` recorded but not applied | challenger read 60 rows past its cutoff | Yes | Yes | 2 | none known |
| F6 | HIGH | Protected windows unenforced for experiments and autoresearch | 400 rows in window → "supported, promising" | Yes | Yes | 3 | unbounded research refused once a window exists (intended) |
| F5 | HIGH | Cache answered before the window check | cached result served after declaration | Yes | Yes | 2 | none known |
| F7 | MEDIUM | Window overlap compared ISO strings | same-day timestamp allowed | Yes | Yes | 2 | none known |
| F2 | MEDIUM | Reclaim UPDATE unguarded: finished item requeued | 2-connection interleave | Yes | Yes | 3 | no heartbeat; bounded by 3600 s vs 900 s |
| F3 | MEDIUM | Comparisons counted cache hits and reruns | 1 look → 5 comparisons | Yes | Yes | 4 | cross-family duplicates only detected (F12) |
| F8 | MEDIUM | Crash retry and refused runs charged to snooping ledger | 2 uses for 1 measurement | Yes | Yes | 2 | none known |
| F10 | MEDIUM | Boundary test counted rows; blind to UPDATE | +1 to a cell, counts equal | Yes | Yes (content-hash test) | 2 | none known |
| F9 | LOW | Leakage guard matched exact keys only; 5 outcome columns unclassified | `Primary_Error` passed | Yes | Yes | 3 | evaluators already structurally closed |
| F11 | LOW | Unhandled crash strands experiment `running` | engine-crash probe | Yes | Detected | control | manual cleanup |
| F12 | LOW | 4 production drafts share one comparison across families | `duplicate_comparisons = 2` | Yes | Detected | existing | human pruning |
| F13 | LOW | Memory-mined hypotheses tested on the same record | design | n/a | Disclosed | existing | needs as-of memory build |
| F14 | LOW | Main split has no purge/embargo | design | n/a | Documented | — | horizon-overlap optimism |
| F15 | LOW | Experiment status follows latest seed | design | n/a | Documented | — | seeds now counted |
| F16 | INFO | Research dates overlap D20 window; different data | rehearsal window | n/a | n/a | isolation test | none |

No CRITICAL findings.

---

## AC. Tests

| Suite | Tests |
|---|---|
| `tests/research/test_research_integrity_25_9d.py` (new) | 39 |
| `tests/experiments`, `tests/autoresearch`, `tests/challengers` (unchanged, all pass) | 83 / 132 / 90 |

No existing test was changed or weakened. Two tests were confirmed to fail on the unfixed code. Full suite: 4,109 OK, 1 skipped.

---

## AD. Files changed

| File | Change |
|---|---|
| `src/experiments/engine.py` | `cohort_digest`, `cohort_span`; cohort before cache; window check; digest in cache key; cutoff bound; distinct-look comparisons |
| `src/challengers/evaluation.py` | same ordering, key and counting |
| `src/autoresearch/governance.py` | instant-based overlap, fail-closed parse, schema-free read, normalised leakage keys |
| `src/autoresearch/queue.py` | guarded reclaim |
| `src/autoresearch/cycle.py` | snooping use only for fresh measurements |
| `src/domain/experiment_models.py`, `src/experiments/api.py` | `cohort_digest` on the run |
| `src/data_access/experiment_schema.py`, `challenger_schema.py` | additive `cohort_digest` column |
| `scripts/audit_research_integrity.py` | new |
| `tests/research/test_research_integrity_25_9d.py` | new |

**Schema:** two additive nullable-default columns. Nothing dropped or rewritten.

---

## AE. Safety

No IBKR connection; no paper, shadow or live order. Unchanged since `9144395`: trading, execution, risk, signals, modeling, portfolio, impact, anchoring, ledger and D20 tooling. Confidence floor 0.40, model gates and anchor-v2 untouched. `audit_live_safety.py`: **16/16**.

---

## AF. Remaining risks

1. **`code_version` is not in the cache key.** An evaluator change can reuse a result. Mitigation: `--no-cache` after evaluator edits.
2. **Protected windows are now strict.** Declaring one refuses unbounded research until `as_of` is set before it.
3. **Memory is not built as-of** (F13).
4. **Four draft experiments duplicate one comparison** (F12). Prune before running them.
5. **These fixes reach the scheduled pipeline only after the branch is merged.**

---

## AG. D20 confirmation (§88)

The protected D20 statistic was not computed, read, approximated or reconstructed. `validate_d20_reversal.py` was not executed. The ledger is byte-identical (`7113fa4e…`), and D20 reads `NOT_CONSUMED`. The frozen hypothesis, anchor-v2, protected window, readiness checker and runbook are unchanged.

---

## AH. Verification

Full suite, rerun cleanly after the last code change: **4,109 tests OK**, 1 skipped (Phase 25.9C: 4,070). `audit_live_safety.py` 16/16. Integrity negative controls 10/10.

---

## AI. Human decisions pending

1. Prune or merge the four duplicate draft experiments (F12).
2. Carried from earlier phases: the 25.9 gate defect, production migration to anchor-v2, the BK/AVB/EA/PARA mappings, and merging this branch.

---

## AJ. Final status

```
PHASE 25.9D STATUS:                    COMPLETE
DATASET IDENTITY:                      PASS
EXPERIMENT IDENTITY:                   PASS
RUN / RESULT IDENTITY:                 PASS
STALE-CACHE REGRESSION:                FIXED
RETRY ≠ NEW EVIDENCE:                  FIXED
QUEUE CONCURRENCY:                     PASS
STALE RECLAIM:                         FIXED
CRASH / PARTIAL-WRITE RECOVERY:        PASS (stranded experiment detected)
LINEAGE:                               PASS
POINT-IN-TIME:                         FIXED
LEAKAGE ADVERSARIAL SUITE:             PASS
PROTECTED-WINDOW OVERLAP:              FIXED
MULTIPLE TESTING:                      FIXED
DATA SNOOPING LEDGER:                  FIXED
NEGATIVE KNOWLEDGE:                    PASS
CONFLICTING EVIDENCE:                  PASS
RESEARCH MEMORY PIT:                   DISCLOSED LIMITATION
CANDIDATE GOVERNANCE:                  PASS
AUTONOMOUS PROMOTION:                  BLOCKED
TRANSITIVE WRITE BOUNDARY:             PASS (measured, negative control detected)
REAL-DATA REHEARSAL:                   PASS (working copy; 0 tables outside scope)
RESEARCH INTEGRITY COMMAND:            OPERATIONAL (10/10 negative controls)
CRITICAL FINDINGS OPEN:                0
HIGH FINDINGS OPEN:                    0
MODEL GOVERNANCE:                      UNCHANGED
CONFIDENCE POLICY:                     UNCHANGED
ANCHOR-V2:                             UNCHANGED
D20 HYPOTHESIS:                        FROZEN
D20 PROTECTED RESULT:                  UNSEEN
D20 PROTECTED TEST:                    NOT EXECUTED
D20 CONSUMPTION:                       UNCONSUMED
PAPER TRADING:                         NOT ATTEMPTED
REAL IBKR ORDER:                       NOT ATTEMPTED
PHASE 25.9E:                           READY
NEXT REQUIRED STEP:                    PHASE 25.9E
```
