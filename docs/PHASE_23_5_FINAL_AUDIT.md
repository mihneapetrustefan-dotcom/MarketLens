# PHASE 23.5 — FULL SYSTEM AUDIT, REMEDIATION & HARDENING

Date: 2026-09-07 · Database audited: **production release asset
`db-latest`** (240 MB, updated 2026-09-06 16:05 UTC) for production
state, and a production-derived copy carrying the Phase 21–23 tables
for behavioural work, because the production asset does not yet contain
them.

---

## PHASE 23 RECOMMENDATIONS REVIEW

Phase 23 closed with seven open items and three recorded repairs. Each
was re-checked against code, database, workflows and tests rather than
against the report.

| # | Recommendation | Status | Evidence |
|---|---|---|---|
| 1 | One 8-day evaluation window, used 4 times | **VERIFIED** | ledger shows `2026-08-26..2026-09-03`, 4 uses, 4 distinct hypotheses |
| 2 | No protected window declared | **VERIFIED** | `autoresearch_protected_windows` empty; the Lab says so and prints the command |
| 3 | Reuse counted at interpretation time only | **VERIFIED (unchanged)** | still true; documented, not fixed — see §50 |
| 4 | Sensitivity sweeps unexercised | **VERIFIED (unchanged)** | available via Phase 22; not run, for the reason in Phase 23 §18 |
| 5 | Research concentration 1.00 | **VERIFIED** | `research_diversity` reports 1.00, 7 areas untouched |
| 6 | Pipeline stages not run under automation | **VERIFIED** | production DB has no `trading_experiences`, `memory_patterns`, `experiments` or `autoresearch_*` table |
| 7 | Exclusion cannot be tested directly | **VERIFIED** | evaluators restrict, cannot exclude; hypothesis wording matches what is measured |

**The three Phase 23 repairs were re-verified as still fixed**: leakage
refusal (`test_researcher_sees_future_data`), absent-result handling
(`test_a_run_that_never_happened_is_not_a_finding`), and observation-id
collision (`test_a_batch_with_colliding_ids_is_refused`).

**One Phase 23 claim did not survive audit — see §4 and §43.1.**

---

## 1. EXECUTIVE SUMMARY

Five defects were found, reproduced, fixed, regression-tested and
verified. Two of them were live correctness problems; one was a
misleading safety claim; two were latent.

| # | Defect | Severity | Status |
|---|---|---|---|
| 1 | **Stale cache served as current research** — a grown dataset produced an identical experiment fingerprint, so a re-run returned the old effect as a cache hit | **CRITICAL** | FIXED, both in Phase 23 and in Phase 22's generator |
| 2 | **The live-safety audit was failing** — `audit_live_safety.py` Q11 flagged Phases 22/23's own tests for asserting the broker's absence | **HIGH** | FIXED and hardened (it also could not see untracked files) |
| 3 | **Boundary test could not see what it certified** — the AST scan reads only SQL inside `src/autoresearch/`, so writes made through Phase 22 were invisible; the report claimed no Phase 22 table was written | **HIGH** (misleading) | FIXED — row-counting test + corrected docs |
| 4 | **Two workers could claim the same queue item** | MEDIUM (latent) | FIXED — atomic claim |
| 5 | **A crashed run stayed RUNNING forever** and was never retried or reported | MEDIUM (latent) | FIXED — stale reclaim |

**Defect 1 is the one that mattered.** Reproduced directly: 300
experiences gave an effect of +0.3333; 150 more arrived; the re-run
reported **+0.3333 as current research on 450 rows**, marked only as a
cache hit. The dataset's identity was purely definitional — `as_of`,
filters, versions — so a record that *grew* hashed identically, and the
run cache keys on that hash. For a project whose central stated
limitation is that its record is short and more data would change the
answer, the cache was hiding precisely that.

Everything else audited clean, including the parts most likely to be
wrong: point-in-time memory, research-graph lineage, the production
boundary, and IBKR safety.

---

## 2. INITIAL STATE

Phase 23 shipped 5,309 lines across 13 modules, 136 tests, and a live
cycle whose numbers the report quoted. The suite passed at 3,577. The
live-safety audit — which no Phase 23 step ran — did not.

---

## 3. AUTONOMOUS RESEARCH AUDIT

All seven Phase 23 subsystems were exercised on real data:
observations (26 from 5 detectors, 6 blind), questions (26, 14
testable), hypotheses (4 after refusing 3 duplicates and 7
unexpressible cohorts), queue, scheduler, conclusions (1 SUPPORTED, 2
INCONCLUSIVE, 1 INSUFFICIENT_DATA), candidates (1, requiring review).
Reproduced end to end after every fix.

---

## 4. AUTONOMY BOUNDARY

**Measured, not asserted.** Row counts of every table in the database
were taken before and after a real cycle. Exactly four tables moved:

```
experiment_results     6 -> 10
experiment_runs        6 -> 10
experiments            6 -> 10
hypothesis_families    6 -> 10
```

No model, signal, memory, outcome, attribution, portfolio, order or
risk table changed.

**The Phase 23 report was wrong** to claim the cycle "writes none of
Phase 22's tables". It writes four, through `templates.ensure_family`,
`engine.save_experiment` and `api.start` — which is correct, because
§27 forbids a second experiment engine. The AST test cited as evidence
scans SQL literals inside `src/autoresearch/` and cannot see a write
performed by calling into another package.

A test that cannot observe the thing it certifies is worse than no
test, because it gets quoted. Replaced with
`test_a_cycle_changes_no_table_outside_research_scope`, which counts
rows and therefore covers transitive calls, plus a negative control
proving the counting method can detect a write. Both documents
corrected.

**CANDIDATE → PRODUCTION does not exist**: no grantable permission, no
function that succeeds, refusals at every entrance.

---

## 5. AGENT PERMISSIONS

| Verb | What the research layer may touch |
|---|---|
| READ | experiences, patterns, attributions, evaluations, experiments, its own tables |
| CREATE | `autoresearch_*` rows; Phase 22 experiments via Phase 22 |
| MODIFY | its own queue/family state; Phase 22 experiment status via Phase 22 |
| DELETE | **nothing** — no DELETE statement exists in the package |
| EXECUTE | registered evaluator names only |

`PROMOTE_CANDIDATE` is defined and ungrantable —
`Grant.researcher().require(PROMOTE_CANDIDATE, ...)` raises. Verified.

---

## 6. TOOL SECURITY

Eleven tools, each declaring a permission.

| Tool | Class |
|---|---|
| `search_memory`, `search_errors`, `search_experiments`, `search_conclusions`, `research_context` | SAFE (read) |
| `create_hypothesis` | SAFE WITH LIMITS (quality gate + leakage check + dedup) |
| `run_research_cycle` | SAFE WITH LIMITS (budget, timeout, claim) |
| `compare_results` | SAFE — deliberately does not rank |
| `promote_candidate` | SAFE — always refuses |

None unsafe, none broken. No tool named `modify_production`,
`submit_order`, `set_risk`, `set_capital` or `update_model` exists.

---

## 7. RESEARCH LOOP

Every transition verified as auditable and as failing safely. The two
that matter:

- **A failed run does not become a finding.** Verified behaviourally by
  seeding a hypothesis whose parameters Phase 22 rejects: no
  conclusion row is written and the queue records the refusal.
- **A cancelled item is not successful.** `experiments_today` counts
  only `state='completed'`; cancelled and rejected items keep their
  reason and are excluded.

Rejected hypotheses remain visible: no DELETE exists anywhere.

---

## 8. INFINITE LOOP PROTECTION

No function in the package calls itself, and nothing schedules on a
timer. A conclusion does not generate a new hypothesis within the same
cycle; the loop is one bounded pass, and `termination_reason` is never
blank (enforced by `integrity_check`). Timeout verified with a 1 ms
budget.

---

## 9. RESEARCH BUDGET

Seven limits, all backend-enforced and all **refusing** rather than
truncating: per-cycle, per-day, per-family, variants, runtime, rows,
concurrency. Daily budget refusal verified.

---

## 10. QUEUE

**Two defects found and fixed.**

`next_batch` only reads. Two workers calling it before either marked
anything RUNNING both selected the same item — verified directly.
Added `claim()`, a single conditional UPDATE whose WHERE clause carries
the expected state, so SQLite picks the winner and exactly one caller
sees `rowcount == 1`. Nothing depends on the caller checking first,
which is the pattern that produced the race.

A worker dying mid-run left its item RUNNING forever: `next_batch`
considers only queued and prioritized, so it was never retried and
never reported — it stopped existing as far as the programme was
concerned. Added `reclaim_stale()`, which returns abandoned items to
the queue **with the reason recorded**, and wired it into the start of
every cycle. A live run is not reclaimed (verified).

Today `max_concurrent_jobs` is 1, so neither changed behaviour. They
are the difference between safe and safe-until-someone-adds-a-worker.

---

## 11. DUPLICATION

Three layers verified working: within a cycle (3 of 7 expressible
claims skipped), against stored hypotheses and Phase 22 experiments,
and within a scheduled batch. On the audited database **all duplicate
comparisons trace to Phase 22's pre-Phase-23 proposals; zero come from
the research layer.**

---

## 12. HYPOTHESIS QUALITY

`quality_problems()` rejects vague wording, missing mechanism, missing
population, empty condition, bad direction, missing evaluator, invalid
falsifiability. `integrity_check` reports zero hypotheses without a
mechanism.

---

## 13. HYPOTHESIS PROVENANCE

Every hypothesis stores `source` and `source_reference`; every
conclusion carries the evidence list forward. Zero orphans (§41).

---

## 14. EXPERIMENT INTEGRITY

Phase 22's integrity check passes on research-created experiments: no
missing mechanism, baseline, dataset snapshot or run, and no run whose
fingerprint moved.

`dataset_version`, `feature_version`, `label_version`, `model_version`
and `strategy_version` are **blank** on research experiments. These are
signal-cohort tests over Phase 21 experiences: no feature set, model or
strategy is varied. **Not fixed now** (§63): populating them changes
`dataset.as_dict()`, therefore the fingerprint, therefore the identity
of every stored experiment — a migration with no correctness benefit,
since `dataset_snapshot_id` and the newly-added `data_cutoff` already
identify the data. Recorded as LOW in §50.

---

## 15. POINT-IN-TIME

`memory_as_of` verified directly on real data:

```
as_of 2026-08-15 → 393 experiences, 224 patterns
as_of 2026-09-05 → 4,311 experiences, 610 patterns
```

The earlier view is strictly smaller. The research cohort loader
respects `as_of`: 4,311 rows unrestricted, 1,121 with a 2026-08-20
cutoff, and **no row past the cutoff**.

---

## 16. LEAKAGE

Phase 23's decision-time refusal re-verified. `primary_error` and the
other outcome-derived fields still raise `LeakageRefused`; unknown
fields are reported rather than guessed, so the guard stays predictable
as the schema grows.

---

## 17. TEST-SET PROTECTION

Implemented and working: overlap is refused, not merely containment,
and a window clipping a protected edge is rejected (verified). **None
is declared on this database** — with 28 days of record, reserving a
meaningful region would leave nothing to research on. Documented, not
invented.

---

## 18. MULTIPLE TESTING

Hypotheses, distinct claims, repeated claims and experiments counted
per family and overall. The distinct-claim count travels with every
conclusion's limitations.

---

## 19. DATA SNOOPING

Ledger verified real after Phase 23 fixed the degenerate `".."` key:
one window, `2026-08-26..2026-09-03`, 4 uses, 4 distinct hypotheses.
`test_set_reuse` fires from the third pass and blocks PROMISING.

---

## 20. SELECTION BIAS

The quality gate requires OOS effect, interval excluding zero, sample,
robustness fraction, complexity ratio and the absence of serious
overfitting shapes — all fixed before the test. Families report **best
and median**. `compare_results` deliberately does not rank.

---

## 21. CONCLUSIONS

All six types reachable; INCONCLUSIVE precedes REJECTED whenever the
evidence could not decide. `promising = 1` with a non-SUPPORTED
conclusion is an integrity failure and reports zero.

---

## 22. NEGATIVE KNOWLEDGE

No DELETE statement targets any research table. Repeated failure lowers
priority through `assess_family` → `RESEARCH_DEPLETED` → skipped by the
scheduler with a reason, still in the queue.

---

## 23. CONFLICTING EVIDENCE

SUPPORTED against REJECTED on the same claim produces
CONFLICTING_EVIDENCE naming both. An earlier INCONCLUSIVE conflicts
with nothing — verified, so silence is not turned into disagreement.

---

## 24. MEMORY INTEGRATION

**No contamination.** A research cycle was run between two identical
`memory_as_of('2026-08-15')` calls: 393 experiences / 224 patterns
before, **identical after**. Phase 21's raw record is never mutated —
confirmed by row counts, not by inspection.

---

## 25. LLM AUDIT

**No LLM is used anywhere.** Code-only scans (comments and string
literals tokenised away) find no `openai`, `anthropic` or
`chat.completions`. The audit trail's `llm` actor count is **0** — the
absence is measured. §31's hallucination question is therefore vacuous
here: every metric comes from Phase 22, and no component can assert a
result.

---

## 26. TOOL AUDIT

Every tool call requires a permission and every write path records an
audit row with actor, action, decision and reason. `MAX_REASON` caps
prose at 1,000 characters and the schema has no column shaped like a
transcript.

---

## 27. CODE EXECUTION SECURITY

No `eval`, `exec`, `compile`, `__import__`, `import_module`,
`os.system`, `popen` or `check_output` in the package. One
`subprocess` call exists — `git rev-parse` in `candidates.py`, stamping
a code version from the repository's own identity, taking no input from
any research row. It is the single documented exception in the scan.

---

## 28. DATABASE SECURITY

No DROP, no schema destruction, no unbounded DELETE. Writes are
confined to eleven `autoresearch_*` tables plus Phase 22's experiment
tables through Phase 22's own functions.

---

## 29. PRODUCTION BOUNDARY

Proven by measurement (§4) and by source parsing. No import of
`src.modeling.promotion`, `src.execution`, `src.risk`,
`src.portfolio.rebalance` or `src.paper`.

---

## 30. IBKR SAFETY

`scripts/audit_live_safety.py` **was failing** when this audit began —
Q11 flagged Phases 22 and 23's own safety tests for containing the
broker's name while asserting its absence. A file-level search cannot
distinguish a prohibition from an implementation.

Fixed by classifying at line level over a small construct window: a
match inside a negative assertion or a forbidden-word list is proving
absence. While testing, a **second and more serious weakness** surfaced
— `git grep` searches only the index, so a broker adapter written but
not yet committed would have passed. Added `--untracked`.

Verified by negative control: a probe file containing
`GATEWAY = "metatrader5"` is now caught (it was not before), and
removing it returns the audit to green.

**ALL 16 QUESTIONS PASS.**

---

## 31. MODEL GOVERNANCE

Research reads `model_evaluations.beats_all_baselines` — Phase 18's
recorded verdict — and never re-derives deployability. No path bypasses
the Phase 18 promotion gate; `scripts/promote_model.py` still requires
an approver and a reason, and nothing in the research layer calls it.

---

## 32. CANDIDATE GOVERNANCE

One candidate on the audited database: `ready_for_review`,
`requires_review = 1`, base version named, review note leading with the
overfitting warnings. `promoted_candidates` reports 0 and no code path
can produce it.

---

## 33. PERFORMANCE

| Stage | Time |
|---|---|
| observe (26 observations) | 0.10s |
| question + triage (26) | 0.88s |
| full cycle (4 experiments) | 8.6s |
| per experiment | 2.15s |

The dominant cost is running Phase 22 experiments, not the research
layer. No bottleneck worth optimising; **no change made**, because
there is no evidence supporting one.

---

## 34. DATABASE

Eleven research tables, nine indexes, additive migration. **Zero
orphans** across the entire research graph — conclusions→hypotheses,
hypotheses→questions, questions→observations, queue→hypotheses,
candidates→conclusions, conclusions→experiments.

Audited by hand and then **made permanent**: five lineage checks added
to `integrity_check`, which now runs 14 checks and reports all zero.

---

## 35. API

Limits clamped by `MAX_LIMIT = 500`; every listing takes a bounded
`limit`. There is deliberately no `successes_only` parameter. No HTTP
layer exists by design (`docs/API_AUDIT.md`), so authentication and
rate limiting are not applicable — stated rather than claimed as
passing.

---

## 36. FRONTEND

All 20 workspaces render with **zero console errors**, verified in a
browser. The Lab shows real state: on a database without research it
reports itself unavailable and prints the command, rather than showing
an empty page that looks like a finished one. No "AI discovered"
language anywhere; the page leads with refusals and unsupported
conclusions.

One collector bug fixed during the audit: availability was gated on the
questions table alone, so a database holding conclusions but no stored
questions would have hidden real findings.

---

## 37. RESEARCH TRANSPARENCY

The conclusion detail page shows question → hypothesis → mechanism →
provenance → experiment → in-sample and out-of-sample effects → gap →
interval → sample → every reason → overfitting warnings → limitations.

---

## 38. AUDIT TRAIL

Every action records timestamp, actor, action, decision, reason and
structured evidence, including reclaim events added by this audit.

---

## 39. REPRODUCIBILITY

Runs are seeded and deterministic. **The stale-cache defect was
precisely a reproducibility failure of the opposite kind** — the same
inputs were *not* the same inputs, and the system could not tell.
Identical data still yields an identical experiment identity (verified,
so caching still works); a longer record now yields a different one.

---

## 40. FAILURE RECOVERY

An abandoned RUNNING item is reclaimed with a reason. A run producing
no result is refused rather than interpreted. Re-running a cycle does
not duplicate hypotheses, conclusions or candidates — every id is
derived from content rather than from the attempt.

---

## 41. DATA LINEAGE

Traceable end to end: market data → features → dataset → model →
prediction → signal → outcome → attribution → memory → observation →
question → hypothesis → experiment → run → result → conclusion →
candidate. Zero broken links, now enforced.

---

## 42. FUTURE LEARNING READINESS

The structured record exists: falsifiable claims with provenance,
conclusions with confidence and warnings, families with best and
median, a snooping ledger and a multiple-testing count. No learning
implemented, as required.

---

## 43. REPAIRS MADE

### 43.1 Stale cache served as current research — CRITICAL

**Evidence.** 300 experiences → effect +0.3333. 150 more added. Re-run
→ identical experiment id, identical fingerprint, `cache_hit=True`,
effect +0.3333 reported on 450 rows.

**Root cause.** `DatasetSnapshot.snapshot_id` derives from `as_of`,
cutoff, universe, filters and versions — all definitional. With
`as_of=None` a *grown* dataset hashes identically. The run cache keys
on `fingerprint + seed`. Phase 23 compounded it by deriving
`experiment_id` from the hypothesis alone, asserting that one claim
maps to one experiment forever.

**Fix.** `engine.current_data_cutoff(conn)` — one definition of how far
the record extends. Phase 23 stamps it into `DatasetSnapshot` and keys
`experiment_id` on hypothesis + cutoff. Phase 22's generator carried
the identical defect and was fixed the same way
(`_stamped_dataset`, `_experiment_id(..., cutoff)`).

**Files.** `src/experiments/engine.py`, `src/experiments/templates.py`,
`src/autoresearch/cycle.py`.

**Tests.** `TestDatasetIdentityAndCache` — three tests: a grown record
is a different experiment and not a cache hit; an unchanged record
keeps one identity (so caching still works); the cutoff is recorded.

**Verification.** Re-run: new id, `cache_hit=False`, effect 0.0000 vs
0.3333. Phase 22 on real data: 5 proposals, cutoff stamped, zero id
overlap after the record extends.

**Risk reduced.** Research results can no longer silently describe a
dataset that no longer exists. It also unblocks §66 reactivation:
re-testing a depleted hypothesis on new evidence now creates a new
experiment instead of colliding with its own history.

### 43.2 The live-safety audit was failing — HIGH

**Evidence.** `AUDIT FAILED — Q11`, naming four test files.
**Root cause.** File-level `git grep` cannot distinguish a prohibition
from an implementation; Phases 22/23 added tests asserting the broker's
absence. **Fix.** Line-level classification over a 3-line construct
window, plus `--untracked`. **Tests.** Negative control (probe
detected, then clean). **Risk reduced.** The broker boundary's last
line of defence is green and now also sees uncommitted work.

### 43.3 Boundary test could not see what it certified — HIGH

**Evidence.** Row counts show four Phase 22 tables change per cycle;
the AST test passed and the report claimed none did. **Fix.**
Row-counting boundary test plus a negative control; `EXPERIMENT_TABLES`
named explicitly; both documents corrected. **Risk reduced.** The
boundary a reader is asked to trust is now measured and written down.

### 43.4 Two workers could claim one queue item — MEDIUM

Verified, fixed with an atomic conditional UPDATE, four regression
tests.

### 43.5 A crashed run stayed RUNNING forever — MEDIUM

Verified, fixed with `reclaim_stale()` wired into every cycle, with the
reason recorded.

---

## 44. IMPROVEMENTS MADE

| Change | Reason | Benefit | Risk | Tests |
|---|---|---|---|---|
| Five lineage checks added to `integrity_check` | audited by hand, found clean | stays clean without re-auditing | none (read-only) | covered by the integrity test |
| Lab availability gated on questions **or** conclusions | a DB with findings but no questions hid them | findings cannot be hidden by a missing table | none | `test_unsupported_conclusions_are_counted_beside_supported` |

---

## 45. TEST RESULTS

**Full suite: 3,586 tests, OK, 1 skipped** (360s). Up from 3,577 —
nine regression tests added, none removed, none weakened.

Per-area: autoresearch 132, experiments 83, dashboard research 13,
dashboard experiments 16.

---

## 46. SECURITY RESULTS

No credentials, no environment access, no shell, no arbitrary
execution, no destructive SQL, no second broker, no live order path.
`audit_live_safety.py`: **16/16 PASS**, verified by negative control.

---

## 47. LEAKAGE RESULTS

Decision-time refusal working; `memory_as_of` monotone and verified; no
row past an applied cutoff; no contamination of historical views by
research.

---

## 48. PRODUCTION READINESS

Unchanged by this audit and not claimed. No live trading, no real-money
capital default, promotion still human.

---

## 49. AUTONOMY READINESS

| Dimension | Score | Why |
|---|---|---|
| Research autonomy | 60 | proposes, triages, tests and concludes without help; six of fifteen observation kinds are blind |
| Research safety | 92 | boundary measured, not asserted; permissions ungrantable; verified by negative control |
| Research reproducibility | 85 | seeded and deterministic; the one failure mode found was fixed and regression-tested |
| Research transparency | 90 | full trace per conclusion; refusals shown with reasons |
| Research quality | 70 | gate demands OOS, interval, robustness, complexity; the record is too short to exercise it properly |
| Resource control | 88 | seven budgets, all refusing; claim and reclaim now correct |
| Boundary safety | 95 | no path exists, and the test now measures rather than infers |

---

## 50. REMAINING ISSUES

**CRITICAL** — none.

**HIGH** — none.

**MEDIUM**

1. **28 days of record, one 8-day evaluation window.** Every conclusion
   carries `narrow_time_period`; `test_set_reuse` binds from the third
   pass. Time is the only fix.
2. **Pipeline stages 13, 14 and 15 have never run under automation.**
   The production database contains no memory, experiment or research
   table. Next scheduled run will create them.

**LOW**

3. **Reuse is counted at interpretation time**, so a conclusion drawn
   on the second pass is not retrospectively re-flagged when the window
   is used a fourth time. *Why not fixed now:* re-interpreting stored
   conclusions would rewrite history to reflect facts that were not
   true when they were drawn; the ledger shows the current count beside
   them, which is the honest presentation.
4. **Feature/label/model/strategy versions blank** on research
   experiments (§14). *Why not fixed now:* changes the fingerprint and
   therefore the identity of every stored experiment, for no
   correctness gain — the dataset is already identified by snapshot id
   and cutoff.
5. **Research concentration 1.00.** Seven of eight areas untouched
   because their evaluators do not exist. A Phase 24 concern.
6. **No static analysis or coverage tooling is configured.** No lint,
   type-checking or coverage config exists in the repository. Stated
   rather than substituted with an invented number, per §67/§68.

---

## 51. FINAL ARCHITECTURE

| Area | Score | Honest reason |
|---|---|---|
| DATA | 88 | ingestion and canonicalisation solid; 28 days is short |
| INTELLIGENCE | 72 | fusion and events work; regime is NULL throughout |
| RESEARCH | 84 | full loop, measured boundary, honest refusals; short record |
| QUANT | 70 | walk-forward, purge, embargo present; little data to use them on |
| MODELS | 55 | one family, none promoted, all below the baseline gate |
| SIGNALS | 74 | canonical, model-linked, status-aware |
| OUTCOMES | 88 | measured, versioned, delisting handled |
| ERROR ATTRIBUTION | 80 | 10,661 attributions; six layers still evidence-less |
| MEMORY | 82 | point-in-time safe, contradiction-preserving, uncalibrated |
| EXPERIMENTS | 88 | criteria frozen in the fingerprint; dataset identity now correct |
| AUTONOMOUS RESEARCH | 84 | five defects found and fixed this phase |
| PORTFOLIO | 45 | defined, no positions exist |
| RISK | 60 | limits exist, unexercised |
| BACKTEST | 75 | reproducible, fingerprinted |
| PAPER | 55 | wired, never run |
| IBKR | 90 | 16/16 safety questions pass, verified by negative control |
| EXECUTION | 58 | no order has ever been placed |
| SECURITY | 92 | no credentials, no execution paths, no second broker |
| OBSERVABILITY | 86 | cycles, actors, budgets, queue depth, snooping ledger |
| API | 78 | complete and bounded; no HTTP layer by design |
| DATABASE | 88 | zero orphans, indexed, additive migrations, now enforced |
| FRONTEND | 84 | 20 workspaces, zero console errors, refusals shown first |

---

## 52. NEXT PHASE

Phase 24 — Challenger Models & Strategy Variants. The candidate
registry carries base version, changes, experiment, conclusion, code
version and review reason, and nothing can promote from it.

The two things actually blocking better research are unchanged and are
not Phase 24's to fix by writing code: evaluators that can express
model, regime and exclusion cohorts, and enough record that a held-out
half is worth holding out.

---

# READY FOR PHASE 24

Five defects found, reproduced, fixed, regression-tested and verified —
including one that returned a stale cached effect as current research,
and one that had left the project's live-safety audit failing.

Two claims that could not survive scrutiny were corrected rather than
defended: the Phase 23 report's assertion that no Phase 22 table is
written, and the AST test that appeared to prove it.

The full suite passes at 3,586. `audit_live_safety.py` passes 16 of 16
and now catches what it previously could not. The research graph has
zero orphans and says so on every run. The autonomy boundary is
measured by counting rows rather than inferred from source.

Nothing in production changed. Interactive Brokers remains the only
broker, live trading stays disabled, no LLM is used, and promotion
remains a human decision.

The project is in a stronger and more trustworthy state than before
this audit, primarily because two things it believed about itself were
not true.
