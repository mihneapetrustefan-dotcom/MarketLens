# PHASE 23 — FINAL REPORT

**Autonomous Research Engine**
Date: 2026-09-07 · Method version `v1`

---

## PHASE 22 RECOMMENDATIONS REVIEW

Phase 22 closed with six open items.

**1. The proposal generator produces duplicates.** **Fixed, and by
construction rather than by patching.** Phase 22 detected duplicates
after the fact; Phase 23 prevents them before anything runs.
`ResearchHypothesis.claim_fingerprint` keys on what a hypothesis
*claims* — population, condition, direction, metric, evaluator,
parameters, baseline — and never on its wording. In the live run, 14 testable
questions produced **4 hypotheses**: 3 were refused as duplicate
claims and 7 as cohorts no evaluator can express, all before an
experiment existed. The scheduler
applies the same key again inside a batch (§25).

**2. 28 days of history.** Unchanged and now the binding constraint on
everything. The out-of-sample window this phase evaluates on is
**8 days** (2026-08-26 to 2026-09-03). Every conclusion carries a
`narrow_time_period` warning, and that is correct.

**3. Six evaluators cannot run.** Unchanged, and it now propagates:
seven of the fourteen testable questions could not be turned into
hypotheses because no registered evaluator can filter on
`trained_model_id`, `strategy_id` or `instrument_id`. They are refused
at hypothesis time with that reason rather than becoming experiments
that fail.

**4. Zero validated experience.** Unchanged. Every hypothesis here was
tested against experimental experience.

**5. The pipeline stage had not run under automation.** Still true, and
now compounded: Phase 22's stage 14 and Phase 23's stage 15 have both
been added since the last scheduled run. Recorded again in §41.

**6. No experiment has passed.** This is the item that changed. One
hypothesis reached SUPPORTED and cleared the quality gate — see §21 and
§30 — on 41 out-of-sample observations, with two overfitting warnings
attached and a candidate that says so in its review note.

---

## 1. EXECUTIVE SUMMARY

Phase 23 built a researcher: something that observes the system's own
record, raises questions, refuses most of them for stated reasons,
turns a few into falsifiable claims, tests them with Phase 22, and
stops at memory.

One live cycle on the production-derived database:

| | |
|---|---|
| observations | 26, from 5 detectors |
| blind spots | 6 detectors that cannot see anything here |
| questions | 26 — **14 testable, 12 refused** |
| hypotheses | 4 formed; 3 duplicate claims and 7 unexpressible cohorts refused |
| conclusions | 4 — 1 SUPPORTED, 2 INCONCLUSIVE, 1 INSUFFICIENT_DATA |
| candidates | 1, requiring review |
| runtime | 15.7s |

**The most valuable thing this phase built is a refusal.** The first
triage run on real data produced two *top-priority* questions asking
whether excluding signals whose `primary_error` is `prediction_error`
improves accuracy. That reads as a sensible research question and is
unimplementable: `primary_error` is Phase 20's verdict about what went
wrong, knowable only after the outcome. A filter cannot consult it.
**46 memory patterns are keyed on that field.** Without the
decision-time check, every one of them was a candidate research
question, and any experiment built from one would have looked superb.

Three defects were found and fixed during the build, one of them the
kind that produces confident-looking nonsense — see §54.

---

## 2. INITIAL STATE

Phase 22 left an experiment engine that could test a hypothesis
somebody wrote, and 6,510 experiences, 610 patterns and 10,661 error
attributions nobody had systematically turned into questions. There was
no notion of an observation, a research question, a triage decision, a
falsifiability record, a research budget, a data-snooping ledger, or a
candidate.

---

## 3. RESEARCH ARCHITECTURE

Observation → question → hypothesis → Phase 22 experiment → result →
conclusion → memory. Thirteen modules, 5,309 lines, plus a 395-line
CLI, in a package named
`autoresearch` because `src/research/` is Phase 6's dataset builder and
`research_observations` already holds rows. Full architecture in
[`PHASE_23_AUTONOMOUS_RESEARCH.md`](PHASE_23_AUTONOMOUS_RESEARCH.md).

---

## 4. RESEARCH OBSERVATIONS

Five detectors run: recurring errors, recurring success, signal
weakness, model degradation, experiment failures. **Six are registered
and blind** — regime dependence, execution behaviour, portfolio
behaviour, feature importance, feature instability, event reaction —
each raising a stated reason rather than returning an empty list.

Phase 22 established why: an empty result flows downstream and reads as
*"we looked and there was nothing"*, which is a different and far more
flattering claim than *"we cannot look"*.

Every observation references evidence. `validate()` refuses one that
does not.

`model_degradation` reads `model_evaluations.beats_all_baselines` —
Phase 18's recorded gate result — rather than re-deriving
deployability. A second definition of "good enough" in the research
layer is how two parts of a system start disagreeing about whether a
model is usable, and the research layer would be the one nobody
thought to check.

---

## 5. RESEARCH QUESTIONS

26 raised, each with a source type, a source id, its evidence, a
priority breakdown and a triage decision. `validate()` requires a
trailing question mark: a statement dressed as a question is usually a
conclusion somebody already reached, and it is the cheapest available
check that the research is still open.

---

## 6. HYPOTHESIS GENERATION

Deterministic templates, one per observation kind, with every number
supplied by the observation — so no generated sentence can assert
something the record does not contain. Where a hypothesis was mined
from the record it will be tested against, the mechanism text says so.

Kinds with no template raise rather than producing something generic.
Better no hypothesis than one that cannot be tested honestly.

---

## 7. HYPOTHESIS QUALITY

`quality_problems()` returns every reason a claim is not yet testable,
as a list, so triage can record all of them. It rejects vague wording,
a missing mechanism, a missing population, an empty condition, an
unrecognised direction, a missing evaluator, and invalid
falsifiability.

§9's own bad example, *"Maybe momentum is bad"*, fails on the first
check.

---

## 8. FALSIFIABILITY

`FalsifiabilityCriteria` records the expected result, minimum effect,
acceptable degradation, minimum sample, whether the interval must
exclude zero, and the metric — all before the test. A minimum effect of
zero is refused, because it makes every outcome a success, which is the
definition of an unfalsifiable claim.

These become the Phase 22 `AcceptanceCriteria` inside the experiment
fingerprint, so once a run starts they cannot move.

---

## 9. RESEARCH PRIORITIZATION

Six weighted components plus two penalties, all stored, plus the total.
**No predicted-profitability component exists** (§11) — not computed,
because a field that exists gets weighted eventually.

A single-instrument observation carries a research-risk penalty: one
instrument's history is one instrument's history however many rows it
holds.

---

## 10. RESEARCH COST

Rows required, variants, estimated seconds, complexity, and a
normalised score feeding the priority penalty. Rough by design — a
precise-looking estimate would be trusted more than it deserves.

---

## 11. DUPLICATE DETECTION

Three layers, all keyed on the claim rather than the wording:

1. within a cycle — 3 of the 7 expressible claims skipped as repeats
2. against stored hypotheses and Phase 22 experiments
   (`find_duplicate`), which returns the *previous answer* rather than
   just refusing
3. within a scheduled batch (`next_batch`)

This closes Phase 22's first open item.

---

## 12. HYPOTHESIS FAMILIES

Phase 22's families, reused. Family identity derives from the *shape*
of a claim — what kind of change, keyed on which fields — so three
differently-phrased tests of one idea land in one family rather than
three. That miscount is exactly what let Phase 22 report three
identical results as three findings.

Four families in the live run, each with one experiment.

---

## 13. MEMORY INTEGRATION

`research_context()` is consulted before anything is queued: has this
claim been tested, what happened, is the family depleted, how many
attempts stand behind it.

**Memory is updated without Phase 23 writing Phase 21's tables.** §39
asks that Trading Memory learn the result. Inserting a research finding
into `trading_experiences` would corrupt the point-in-time property
every later phase depends on — an experience has an `available_at`
marking when it became knowable, and a finding has neither. So the
conclusion row *is* the update, and `memory_feedback(pattern_id)` reads
back every conclusion about a pattern, failures included. A test
asserts the experience count is unchanged after a cycle.

---

## 14. RESEARCH QUEUE

Seven states. Rejected and cancelled items are written and kept: a
queue holding only work it intends to do cannot answer *"why was this
never tested"*, which is the question a research record most often
needs to answer.

---

## 15. RESEARCH SCHEDULER

`next_batch` sorts by priority and returns `(selected, skipped)`. Both
halves, always — a scheduler that silently drops work is
indistinguishable from one with none. Skips carry reasons: depleted
family, per-family cap, identical claim, cycle budget committed.

Nothing here starts on a timer, and no function calls itself.

---

## 16. EXPERIMENT PROPOSALS

Phase 22's engine, unchanged. There is no second experiment system, no
second splitter and no second backtester (§27). `build_experiment`
constructs a Phase 22 `Experiment` and hands it over.

---

## 17. SEARCH SPACES

An evaluator is a **registered name** looked up in Phase 22's registry.
The configuration cannot express code at all, which is how §29 is
enforced structurally rather than by validation. A cohort condition
that no evaluator can filter on is refused at hypothesis time — 7 of 14
testable questions in the live run.

---

## 18. PARAMETER SEARCH

Phase 22's `sensitivity` sweep, bounded by `max_variants`. Not exercised
in the live cycle: a sweep needs neighbours measured, and with one
8-day window the reuse ledger makes repeated sweeps the wrong thing to
spend the test set on. Recorded as an open item (§54).

---

## 19. RESEARCH SANDBOX

The research layer reads production tables and writes eleven
`autoresearch_*` tables. An AST scan over every SQL literal handed to
`execute` proves the write set is a subset of those — with a
word-boundary parser, after a substring version misread a SELECT
listing `updated_at` as an UPDATE.

---

## 20. PRODUCTION BOUNDARY

Enforced by absence, tested by parsing:

- no module writes any Phase 6/19/20/21 table, and the only Phase 22
  tables written are the experiment tables, through Phase 22's own
  functions (corrected in Phase 23.5; see the closing section)
- no module imports `src.modeling.promotion`, `src.execution`,
  `src.risk`, `src.portfolio.rebalance` or `src.paper`
- no `place_order`, `submit_order`, `set_capital`, `.fit(`, `def train`
- `CandidateStatus.PROMOTED` is named only where it is refused
- `candidates.promote()` and `tools.promote_candidate()` both always
  raise, and exist so the refusal is findable

---

## 21. RESULT INTERPRETATION

Metrics are **not recomputed**. Phase 22 measured the effect, interval
and robustness; a second computation would eventually disagree and then
neither could be trusted. Phase 23 adds interpretation only.

The live results:

| conclusion | OOS | in-sample | interval | n |
|---|---|---|---|---|
| SUPPORTED — restrict to `event_type=acquisition, horizon=3d` | **+0.1515** | +0.0481 | [+0.0052, +0.2924] | 41 |
| INCONCLUSIVE — strength floor ≥ 0.5 for `prediction_error` | +0.0145 | −0.0075 | [−0.0781, +0.1108] | 126 |
| INCONCLUSIVE — `acquisition` + long underperforms | −0.0669 | −0.0032 | [−0.2122, +0.0807] | 46 |
| INSUFFICIENT_DATA — restrict to `short, 15m` | −0.0558 | +0.1367 | n/a | 28 |

The last row is worth reading twice: an in-sample effect of **+13.67%**
and an out-of-sample effect of **−5.58%**, on 28 observations. It is
reported as INSUFFICIENT_DATA rather than as anything else, because 28
is below the 30 fixed before the test.

---

## 22. OVERFITTING DEFENSE

Eight shapes from §19, reported as facts rather than scored. Every live
conclusion carries `narrow_time_period` and `single_regime`; the fourth
carries `test_set_reuse`.

`narrow_time_period` fires because the record span is **measured** from
the database rather than passed in by the caller — the caller being the
party with an interest in it looking large.

---

## 23. MULTIPLE TESTING

Hypotheses, distinct claims, repeated claims and experiments, per family
and overall. The distinct-claim count is the one that matters: a family
count called three identical Phase 22 experiments three attempts when
they were one.

---

## 24. DATA SNOOPING

`autoresearch_window_usage` counts touches per evaluation window. The
live ledger holds **one window, 2026-08-26 to 2026-09-03, used 4 times
by 4 hypotheses.**

An earlier version recorded every use under the key `".."` because no
window was computed — it counted "some experiment ran" rather than
"this period was tested again", and would never have distinguished two
genuinely different periods. The window is now derived the same way
Phase 22 derives its split, so there is no second definition of "out of
sample".

---

## 25. TEST-SET GOVERNANCE

`autoresearch_protected_windows` reserves regions from the autonomous
researcher; overlap is refused, not merely containment. **None is
declared on this database**, and the Lab says so with the command to
declare one. With 28 days of record there is not enough to reserve a
meaningful region without leaving nothing to research on — recorded as
an open item.

---

## 26. RESEARCH BUDGET

Seven configurable limits. Every one **refuses** rather than truncating:
a budget that silently trimmed a sweep would answer a different question
than the one asked and the reader could not tell.

---

## 27. NEGATIVE RESULTS

3 of 4 live conclusions are not SUPPORTED, all stored, counted and
shown. There is no delete path anywhere in the package and no
`successes_only` parameter on any API — a research record filtered to
its successes is not a record.

---

## 28. INCONCLUSIVE RESULTS

INCONCLUSIVE is reached **before** REJECTED whenever the evidence could
not decide. Two live results are inconclusive because their intervals
include zero, which says the data cannot separate candidate from
baseline — not that the mechanism is wrong. Collapsing the two loses the
more useful finding.

---

## 29. CONFLICTING RESULTS

A prior SUPPORTED against a new REJECTED on the same claim produces
CONFLICTING_EVIDENCE with the two named, never averaged. An earlier
INCONCLUSIVE conflicts with nothing: treating an absence of finding as
disagreement would manufacture conflict out of silence.

---

## 30. RESEARCH CONCLUSIONS

Six types, five of them ways of not having found something. Every
conclusion carries reasons — including the checks that passed — and
`validate()` refuses one with none.

Confidence is ordinal (HIGH/MEDIUM/LOW/INSUFFICIENT) and deliberately on
a separate scale from model, signal and memory confidence, so the four
cannot be confused or multiplied.

---

## 31. CANDIDATE REGISTRY

One candidate, `READY_FOR_REVIEW`, `requires_review = 1`. Its review
note leads with the uncomfortable clauses:

> overfitting shapes present: narrow_time_period, single_regime ·
> measured on 41 out-of-sample observations · PROMISING means the
> criteria fixed before the test were met. It is not an approval to
> deploy, and no part of production has changed.

A review note that lists only a candidate's merits is a recommendation
wearing a review's clothes.

---

## 32. CANDIDATE VERSIONING

Every candidate names its base version, its changes, the experiment, the
code version, and the conclusion. `validate()` refuses one with no base:
a change with no base cannot be reproduced or reviewed.

---

## 33. ROBUSTNESS

Phase 22's time slices, with the fraction in the verdict. The quality
gate requires 60%.

---

## 34. SENSITIVITY

Available through Phase 22's sweep, with three shapes including
`no_effect`. Not exercised in the live cycle — see §18 and §54.

---

## 35. COMPLEXITY

Phase 22's complexity ratio, capped at 5× in the gate. §59's simplicity
preference is implemented as that ceiling rather than as a tie-break,
and the principle is documented rather than silently encoded.

---

## 36. RESEARCH DIVERSITY

Concentration **1.00** — every hypothesis tested is a signal-cohort
test. Seven of eight research areas are untouched. That is reported on
the Lab page rather than smoothed over: a programme testing one kind of
idea should be able to see that about itself.

Exploration ratio 1.00 — four families, each tested once. Portfolio
bookkeeping only; nothing adjusts a policy or a reward, because that
would be the reinforcement learning §0 forbids.

---

## 37. LLM ROLE

**None.** §47 permits an LLM and does not require one. Every generator
is deterministic, which keeps the no-LLM property Phases 21 and 22
enforce, keeps credentials out of the research path, and means every
claim traces to a row rather than to a generated sentence.

`Actor.LLM` exists so that if one is added its actions are
distinguishable from the first row rather than retrofitted. The audit
summary reports **llm: 0** — the absence is measured, not asserted.

---

## 38. AGENT TOOLING

Eleven tools, each declaring a permission. Reads, creation, a bounded
cycle, a comparison that deliberately does not rank, and one that always
refuses.

---

## 39. AGENT SECURITY

No shell, no filesystem, no credentials, no environment access, no
`eval`/`exec`/`compile`/`__import__`/`import_module`, no broker other
than IBKR mentioned anywhere, no live order path. The single
`subprocess` call is `git rev-parse` in `candidates.py`, stamping a code
version from the repository's own identity, taking no input from any
research row — and it is the one documented exception in the scan.

---

## 40. RESOURCE LIMITS

Cycle budget, daily budget, per-family cap, variant cap, runtime
timeout, row cap, concurrency. The timeout is honoured mid-loop and
leaves remaining items queued.

---

## 41. OBSERVABILITY

Cycles, observations, questions, hypotheses, conclusions, candidates,
runtime, queue depth by state, and actions by actor and action type.

**Neither Phase 22's stage 14 nor Phase 23's stage 15 has yet run under
automation.** Both were added after the last scheduled pipeline run.
Carried forward as the one open verification item, in the same terms
Phases 21 and 22 used.

---

## 42. AUDIT TRAIL

Every action records actor, action, decision, reason and structured
evidence. `MAX_REASON` caps prose at 1,000 characters so nobody is
tempted to paste a reasoning transcript into it, and the schema has no
column shaped like one (§81).

---

## 43. DASHBOARD

The **Cercetare autonomă** workspace replaces the old research stub. It
leads with the triage breakdown and the refusals *with their reasons*,
then conclusions, then candidates, then families with best **and**
median, then the snooping ledger, protected windows, cycles and actors.

A conclusion detail page shows the full §37 trace: question →
hypothesis → mechanism → provenance → experiment → effects → interval →
every reason → warnings → limitations.

Verified in a browser: all 20 views render, zero console errors.

---

## 44. API

`questions`, `hypotheses`, `research_queue`, `cycles`, `conclusions`,
`candidates`, `families`, `governance_report`, `observability`,
`integrity_check`, plus creation endpoints — the §82 routes, in the
project's established connection-passing style.

---

## 45. DATABASE

Eleven `autoresearch_*` tables with an additive column migration.
Phase 22's structures are reused, not duplicated (§83).

---

## 46. TESTING

**136 new tests, all passing.** Full suite **3,577, OK, 1 skipped**,
402s.

| File | Tests |
|---|---|
| `tests/autoresearch/test_research_loop.py` | 72 |
| `tests/autoresearch/test_boundary_and_safety.py` | 42 |
| `tests/autoresearch/test_pipeline_position.py` | 9 |
| `tests/test_dashboard_research.py` | 13 |

---

## 47. ADVERSARIAL TESTING

All eighteen §74 cases have a named test. Two caught real bugs in this
phase: *researcher sees future data* (§54.1) and *tiny sample becomes
strong conclusion* (§54.2).

---

## 48. SECURITY

No credentials, no secrets, no arbitrary execution, no destructive
database access, no delete path. The safety scans read **code only** —
comments and string literals are tokenised away — after the first
version failed against this package's own docstrings explaining why it
does not use the very words being searched for. A scanner that cries
wolf is one people switch off.

---

## 49. IBKR SAFETY

Unchanged and untouched. Interactive Brokers remains the only broker;
no module references any other, none reaches an order path, and the
research environment is RESEARCH only — not even PAPER is wired.

---

## 50. FILES CREATED

`src/domain/autoresearch_models.py`,
`src/data_access/autoresearch_schema.py`,
`src/autoresearch/{__init__,observations,questions,hypotheses,prioritization,governance,queue,cycle,candidates,tools,audit,api}.py`,
`scripts/run_research.py`,
`tests/autoresearch/{__init__,test_research_loop,test_boundary_and_safety,test_pipeline_position}.py`,
`tests/test_dashboard_research.py`,
`docs/PHASE_23_AUTONOMOUS_RESEARCH.md`, `docs/PHASE_23_FINAL_REPORT.md`.

## 51. FILES MODIFIED

`src/dashboard.py` (collector, workspace, detail page, nav; the
`research` stub replaced), `.github/workflows/pipeline.yml` (stage
15/16, renumbering, reporting).

## 52. FILES REMOVED

None.

## 53. MIGRATIONS

`_add_missing_columns` adds `triage_reason` and `warnings_json` to
pre-existing tables. Additive, nullable, defaulted; old rows stay valid.

---

## 54. REMAINING ISSUES

**Three defects found and fixed during the build**, recorded because
each is a shape worth recognising:

1. **The researcher wanted to see the future.** Two top-priority
   questions proposed filtering on `primary_error`, an
   outcome-derived field. 46 patterns are keyed on it. Fixed with the
   decision-time check; the questions are now UNTESTABLE with a stated
   leakage reason.

2. **A run that never happened was reported as a finding.** Four
   experiments Phase 22 correctly refused to execute produced no
   result, and the cycle interpreted that absence as
   INSUFFICIENT_DATA — *"we tested and the sample was too small"* —
   for something never tested. Fixed at both ends: unexpressible
   cohorts are refused at hypothesis time, and an absent result now
   produces a queue refusal and no conclusion.

3. **Silent row loss with a confident count.** Four model-degradation
   observations shared an id, so 26 observations became 23 rows while
   `save()` reported 26. Fixed by keying on the evaluation and by
   refusing a batch whose ids collide.

**Open:**

1. **One 8-day evaluation window**, used 4 times. Every result carries
   `narrow_time_period`, and `test_set_reuse` becomes binding after
   three passes. Time is the only fix.
2. **No protected window is declared.** With 28 days of record,
   reserving a meaningful region would leave nothing to research on.
3. **Reuse is counted at interpretation time**, so a result concluded
   on the second pass is not retrospectively flagged when the window is
   used a fourth time. The ledger shows the current count; the stored
   conclusion shows the count as it was.
4. **Sensitivity sweeps are available but unexercised**, for the reason
   in §18.
5. **Research concentration is 1.00** — seven of eight areas untouched,
   because the evaluators for them do not exist.
6. **Neither Phase 22's nor Phase 23's pipeline stage has run under
   automation.**
7. **Exclusion cannot be tested directly.** The evaluators restrict to
   a cohort; they cannot exclude one. The weakness hypothesis is
   therefore phrased as "this cohort continues to underperform", which
   is what is actually measured, rather than as "excluding it helps".

---

## 55. FUTURE CHALLENGER MODEL READINESS

Phase 24 inherits a candidate registry where every entry names its base
version, its changes, its experiment, its conclusion, its code version
and why a human should look at it — plus a family record reporting best
and median, a snooping ledger, and a multiple-testing count. What it
does **not** inherit is any path into production: `PROMOTE_CANDIDATE`
is defined, ungrantable, and refused at every entrance.

---

## 56. FUTURE PAPER TRADING READINESS

Nothing is prepared for automatic dispatch to IBKR Paper, deliberately.
A candidate is a record; turning one into a paper strategy is Phase 24's
work and a human's decision.

---

## 57. NEXT PHASE

Phase 24 — Challenger Models & Strategy Variants. The useful first work
is not more research: it is the two things blocking it — evaluators that
can express model, regime and exclusion cohorts, and enough record that
a held-out half is worth holding out.

---

# READY FOR PHASE 24

Phase 23 built a researcher and ran it. It made 26 observations, named
6 things it cannot see, raised 26 questions and **refused 12 of them
with stated reasons**, formed 4 hypotheses from 14 testable questions —
refusing 3 as duplicate claims and 7 as cohorts no evaluator can
express — and reached 4 conclusions of which **3 are not supported**.

The one supported result rests on 41 out-of-sample observations in an
8-day window, and its candidate says so in the first clause of its
review note.

The most valuable thing it built is a refusal: two top-priority
questions wanted to filter on a field knowable only after the outcome,
and 46 memory patterns are keyed on it.

It changes no model, strategy, threshold, feature, sizing, risk limit,
execution setting or capital figure. It writes no Phase 6, 19, 20 or 21
table.

**Correction (Phase 23.5).** This section originally claimed the cycle
writes none of Phase 22's tables either. That was wrong: running an
experiment necessarily writes `experiments`, `experiment_runs`,
`experiment_results` and `hypothesis_families`, through Phase 22's own
functions, because §27 forbids building a second engine. The AST test
cited as proof scanned only SQL literals inside `src/autoresearch/`
and so could not see writes made by calling into another package. The
behaviour was always correct; the claim and its evidence were not.
A row-counting boundary test now measures what actually moves.

Interactive Brokers remains the only broker, live trading stays
disabled, no LLM is used, and promotion remains a human decision —
defined, ungrantable, and refused at every entrance.

A researcher must be able to discover that it is wrong. This one spent
most of its first cycle doing exactly that.
