# Phase 24 — Challenger Models & Strategy Variants

How MarketLens decides whether a promising research idea is *actually*
better than what it already has.

---

## Candidate ≠ Challenger

| | Candidate (Phase 23) | Challenger (Phase 24) |
|---|---|---|
| evidence | one experiment, one chronological split | walk-forward, time/instrument/horizon slices, sensitivity |
| baseline | a registered evaluator | a **named, versioned** baseline pinned into the fingerprint |
| identity | a research finding | an independently versioned implementation |
| output | "worth testing properly" | a six-dimension scorecard and a verdict |

**Not every candidate becomes a challenger** (§3). `validate_candidate`
refuses the ones whose evidence cannot support the extra work, because
building one costs folds, slices and a sweep.

---

## The flow

```
Phase 23 candidate
    → validate  (refuse the ones that cannot support it)
        → challenger definition (versioned, baseline pinned)
            → queue (atomic claim)
                → evaluation (walk-forward, slices, sensitivity)
                    → scorecard (six dimensions, no total)
                        → decision
                            → HUMAN REVIEW
                                → PAPER_CANDIDATE
```

There is no arrow after `PAPER_CANDIDATE`. It is a label a person
applies; it executes nothing and changes no production setting.

---

## The scorecard is not a score

Six named dimensions — **performance, risk, robustness, stability,
complexity, evidence** — and deliberately **no total**, no `overall`
property, no ordering.

§37 forbids collapsing a comparison into one profitability number, and
the reason is mechanical rather than philosophical: a sortable
challenger list gets sorted, and the top of a list of a hundred is
where the noise collects. A reader who wants to rank must decide the
trade-off themselves, in the open.

`test_the_scorecard_has_no_total_and_cannot_be_sorted` asserts that
`sorted([card, card])` raises `TypeError`.

---

## Five verdicts, one of which says "better"

`SUPERIOR` · `INFERIOR` · `INCONCLUSIVE` · `CONTEXT_DEPENDENT` ·
`REQUIRES_REVIEW`

The order of checks in `decide()` is deliberate:

1. **Not enough evidence → INCONCLUSIVE**, before anything else. A
   large effect on a small sample is not an inferior result, it is an
   unmeasured one.
2. **Slices disagree → CONTEXT_DEPENDENT**, before a global verdict.
   A challenger that wins in three contexts and loses in three has told
   you *where* the change helps; averaging destroys that.
3. **Behind the baseline → INFERIOR** (or INCONCLUSIVE if the interval
   spans zero, because then the shortfall itself is not established).
4. **An unmeasured dimension → REQUIRES_REVIEW.** A dimension that was
   never measured is not a dimension that passed.
5. **Every dimension measured and favourable → SUPERIOR.**

---

## What the record can currently support

On this database the record spans **29 days**, which is shorter than a
single walk-forward window. So `stability` reads **unmeasured** on
every challenger, and `SUPERIOR` is therefore unreachable — the honest
outcome is `REQUIRES_REVIEW`.

That is the system working. The alternative — treating an unmeasured
dimension as passed — was the first version's behaviour, and it
produced a `SUPERIOR` verdict whose reasons list read *"met: stability
— walk-forward could not be run on this record"*.

---

## Immutability and versioning

`Challenger.fingerprint` covers the variant type, the **baseline
identity including its version**, the change definition, the evaluation
plan and the **dataset cutoff**.

- A started challenger refuses a changed definition (`ChallengerChanged`).
- `new_version()` is the supported path; the previous version keeps its
  results and its history.
- A grown record produces a different fingerprint, so it is a different
  challenger rather than the same one with different numbers — the
  Phase 23.5 stale-cache lesson, closed by construction.

---

## Challenger types

`SIGNAL` is runnable end to end. `MODEL`, `FEATURE`, `STRATEGY`,
`REGIME`, `PORTFOLIO`, `RISK` and `EXECUTION` are declared and not
deeply implemented, because the infrastructure beneath them does not
exist yet:

| Type | Why not runnable here |
|---|---|
| MODEL | no model has been promoted, so there is no active-model baseline |
| REGIME | `market_regime` is NULL on every experience |
| PORTFOLIO / RISK | no portfolio or position exists |
| EXECUTION | no order has ever been placed |
| FEATURE / STRATEGY | the registered evaluators are signal-level |

Declaring them keeps the gap visible and named — the pattern Phases 22
and 23 both settled on.

---

## What is reused, not rebuilt (§11, §73)

| Concern | Owner |
|---|---|
| purged/embargoed walk-forward splitter | Phase 9 |
| backtester | Phase 12 |
| model deployability gate | Phase 18 |
| point-in-time cohort loading | Phases 21 + 22 |
| evaluators, baselines, bootstrap, economic significance | Phase 22 |
| protected windows, snooping ledger, multiple testing | Phase 23 |
| atomic queue claim, stale reclaim | Phase 23.5 |

Phase 24 orchestrates them and forms a verdict. There is no second
backtester, splitter, baseline registry or bootstrap.

---

## Human review is the only exit

`review()` requires a named `reviewer` and a `reason`, both with no
defaults — exactly as Phase 18's `promote()` does, because an approval
nobody signed is not an approval. Reviews are **append-only**: changing
your mind writes a second row.

Approval cannot be given to a challenger that was never evaluated, or
to one the latest comparison found `INFERIOR`.

`workflow.promote_to_production()` exists and always refuses. It is
there so that somebody searching for how a challenger reaches
production finds an explicit refusal rather than nothing.

---

## Files

| File | Lines |
|---|---|
| `src/domain/challenger_models.py` | 908 |
| `src/data_access/challenger_schema.py` | 290 |
| `src/challengers/registry.py` | 539 |
| `src/challengers/evaluation.py` | 649 |
| `src/challengers/workflow.py` | 495 |
| `src/challengers/api.py` | 410 |
| `scripts/run_challenger.py` | 413 |
| tests | 1,385 (103 tests) |

---

## What this phase cannot do

- promote anything to production — no state, no outcome, no code path
- change a model, strategy, threshold, risk limit or capital figure
- write any Phase 6/19/20/21/22 table
- place an order or reach IBKR at all
- execute arbitrary code — components are registered names
- read credentials or the environment
- delete a challenger, a run, a result or a review
- reach `PAPER` without a named person recording a reason
