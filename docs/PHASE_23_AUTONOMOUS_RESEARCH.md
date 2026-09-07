# Phase 23 — Autonomous Research Engine

How MarketLens turns its own record into questions, turns a few of
those into falsifiable claims, tests them, and stops.

---

## The loop

```
observation (with evidence)
    → question (triaged, with the reason)
        → hypothesis (falsifiable, quality-gated)
            → Phase 22 experiment
                → result
                    → conclusion
                        → memory
```

There is no arrow after **memory**. A promising conclusion becomes a
`ResearchCandidate` — a record with `requires_review = 1` — and that is
the last thing this phase does. §34 is the whole design: a result may
become a CANDIDATE and never automatically an ACTIVE anything.

---

## Naming: why `autoresearch`

`src/research/` and the tables `research_observations`,
`research_features`, `research_labels` already exist. They are Phase 6's
modelling dataset and mean something entirely different. A Phase 23
module named `research_questions` sitting beside them would be read as
part of that system, and `research_observations` already holds rows.

So the autonomous researcher gets its own package and an
`autoresearch_` table prefix. Uglier, unambiguous — the right trade at
this point in a project's life.

---

## The four defences

### 1. Decision-time leakage (§74)

A cohort defined on a field that only exists *after* the outcome cannot
be a filter. `governance.assert_decision_time` refuses them.

This is not hypothetical. The first triage run on real data produced
two **top-priority** questions asking whether excluding signals whose
`primary_error` is `prediction_error` improves accuracy. That reads as
reasonable and is unimplementable: `primary_error` is Phase 20's
verdict about what went wrong, knowable only once the outcome is in.
**46 of this database's memory patterns are keyed on it.** Without the
check, every one was a research question.

### 2. Protected windows (§23)

`autoresearch_protected_windows` holds regions the autonomous
researcher may not evaluate on at all. Overlap is refused, not just
containment — a cohort clipping the edge has still seen part of it, and
"only a little" is not a property that survives repetition.

### 3. Data snooping (§22)

`autoresearch_window_usage` counts how often each evaluation window has
been tested. A researcher permitted to retune against the same held-out
period will eventually find something, and no correction applied
afterwards undoes it. On the current record there is **one** window,
and it is why `test_set_reuse` becomes the binding constraint quickly.

### 4. Multiple testing (§21)

Hypotheses, distinct claims and repeated claims are counted per family
and overall. The distinct-claim count is the one that matters: Phase 22
found three differently-named experiments making a single comparison,
and a family count called that three attempts when it was one.

---

## What a hypothesis must have

| | |
|---|---|
| **mechanism** | why the effect would exist, not just that it might |
| **population + condition** | about what, under what |
| **direction** | increase, decrease, or change |
| **falsifiability** | minimum effect, minimum sample, acceptable degradation, whether the interval must exclude zero — all fixed *before* the test |

§9's own bad example — *"Maybe momentum is bad"* — fails on all four.
`_VAGUE_TERMS` catches "maybe" before anything else is considered.

The falsifiability record becomes the Phase 22 `AcceptanceCriteria`,
which lives inside the experiment fingerprint. Once a run starts, what
counts as success cannot move — and that guarantee is Phase 22's, not a
new one.

---

## Triage: seven ways to say no

| State | Meaning |
|---|---|
| `TESTABLE` | the only state that may become a hypothesis |
| `INSUFFICIENT_DATA` | fewer than 30 observations |
| `UNTESTABLE` | leakage, or no evaluator can express it |
| `DUPLICATE` | already asked — go read the answer |
| `LOW_PRIORITY` | real, not worth the budget now |
| `IGNORED` | deliberately set aside |
| `QUEUED` / `RESEARCHING` | in flight |

Every question stores its triage **reason**. "We looked at this and
decided not to test it, because the cohort is 14 rows" is a finding;
silence is not.

---

## Six ways research ends

`SUPPORTED` · `PARTIALLY_SUPPORTED` · `REJECTED` · `INCONCLUSIVE` ·
`INSUFFICIENT_DATA` · `CONFLICTING_EVIDENCE`

Five of the six are ways of not having found something, and the ratio is
deliberate. `INCONCLUSIVE` is reached **before** `REJECTED` whenever the
evidence could not decide — an interval spanning zero says the data
cannot separate the candidate from its baseline, which is not the same
as the mechanism being wrong.

Conflicting results are **not averaged** (§44). A prior `SUPPORTED`
against a new `REJECTED` on the same claim produces
`CONFLICTING_EVIDENCE`, to be investigated by period, regime,
instrument or methodology. An earlier `INCONCLUSIVE` conflicts with
nothing — treating an absence of finding as a disagreement would
manufacture conflict out of silence.

---

## Priority is a vector

Six stored components — evidence strength, sample adequacy, novelty,
weakness relevance, confidence, minus cost and research-risk penalties —
plus the total. Storing only the total would make the ordering
unarguable, and a research priority nobody can argue with is one nobody
will correct.

**There is no predicted-profitability component.** §11 forbids ranking
on it alone, and the reliable way to obey that is not to compute the
quantity: a field that exists gets weighted eventually.

---

## Families, dead ends, and the median

`autoresearch_family_state` reports **best and median effect** together.
§20 forbids selecting only the best experiment, and the honest
enforcement is making the median impossible to avoid seeing. A family
whose best is +0.4% and whose median is −0.2% is a weak family; either
number alone would not say so.

A family with `DEPLETION_THRESHOLD` conclusions and none supported
becomes `RESEARCH_DEPLETED` and is skipped by the scheduler — with a
reason, still in the queue. §66 allows reactivation when the record it
failed against has materially changed. Depletion is a scheduling
decision, not a claim that the idea is false.

---

## No LLM

§47 permits an LLM as a reasoning layer; it does not require one. Every
generator here is deterministic: an observation maps to a question by
rule, a question to a hypothesis by template, and a hypothesis to a
Phase 22 experiment by registered evaluator name.

That keeps the "no LLM anywhere" property Phases 21 and 22 enforce by
AST scan, keeps credentials out of the research path entirely (§75), and
means every claim traces to a row rather than to a generated sentence.
The audit trail counts actions by actor, and the `llm` row reads zero —
**the absence is measured, not asserted.**

---

## Permissions

`READ_RESEARCH` · `CREATE_HYPOTHESIS` · `CREATE_EXPERIMENT` ·
`RUN_EXPERIMENT` · `READ_RESULTS`

`PROMOTE_CANDIDATE` is defined and **cannot be granted by anything in
this phase**, including `Grant.researcher()`. `tools.promote_candidate`
and `candidates.promote` both exist and both always refuse — they are
there so that somebody searching for how a candidate reaches production
finds an explicit refusal rather than nothing.

---

## The pipeline stage

Stage **15/16**: `run_research.py --questions --apply`. It observes and
triages. It does **not** run experiments, and a test fails if `--cycle`
ever appears in the workflow.

That limit is specific to this database rather than general caution.
Every experiment here evaluates on the same held-out window, because the
record is short enough that there is only one. A scheduled job testing
hypotheses against it twice a day drives the snooping count up every
run, and by the third pass the reuse warning correctly blocks every
result from being promising. Automated testing would spend the one test
set the project has and return nothing usable.

---

## Files

| File | Lines |
|---|---|
| `src/domain/autoresearch_models.py` | 985 |
| `src/data_access/autoresearch_schema.py` | 357 |
| `src/autoresearch/observations.py` | 575 |
| `src/autoresearch/questions.py` | 332 |
| `src/autoresearch/hypotheses.py` | 412 |
| `src/autoresearch/prioritization.py` | 356 |
| `src/autoresearch/governance.py` | 287 |
| `src/autoresearch/queue.py` | 253 |
| `src/autoresearch/cycle.py` | 763 |
| `src/autoresearch/candidates.py` | 223 |
| `src/autoresearch/tools.py` | 293 |
| `src/autoresearch/audit.py` | 112 |
| `src/autoresearch/api.py` | 361 |
| `scripts/run_research.py` | 395 |
| tests | 1,727 (136 tests) |
| **total** | **5,704 excluding tests** |

---

## What this phase cannot do

- promote anything — no grantable permission, no code path, refusals at
  every entrance
- modify a model, strategy, threshold, risk limit or capital figure
- write any Phase 6, 19, 20 or 21 table (it does write Phase 22's
  experiment tables, through Phase 22's own engine — §27 requires
  reusing it rather than building a second one; corrected in Phase
  23.5, where a row-counting test replaced an AST scan that could not
  see transitive writes)
- place an order, or reach IBKR at all
- execute arbitrary code — every evaluator is a registered name
- read credentials or the environment
- delete a conclusion, a hypothesis or a candidate
- loop without a budget and a recorded termination reason
