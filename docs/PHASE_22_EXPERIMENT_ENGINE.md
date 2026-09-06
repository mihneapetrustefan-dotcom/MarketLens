# Phase 22 — Experiment Engine & Hypothesis-Driven Research

How MarketLens turns an observation into a question, and a question
into an answer it is allowed to trust.

---

## What this phase is for

Phase 21 built memory: 6,510 experiences and 610 patterns describing
what has co-occurred. A pattern is a **co-occurrence**, not a cause,
and it says nothing about what will happen next.

Phase 22 is the apparatus for finding out. An experiment states a
hypothesis with a mechanism, fixes what would count as success
*before* running, compares a candidate against a named control on data
split by time, and reports an out-of-sample effect with an interval —
plus every reason behind the verdict.

**PASS means the predefined criteria were met.** It does not mean
profitable, it does not mean deployable, and nothing here promotes
anything. Promotion stays a human decision under Phase 18's gate.

---

## The five rules the engine enforces structurally

These are not conventions someone has to remember. Each is enforced by
code or by a test that parses this package's source.

### 1. Success is defined before the answer is visible

`AcceptanceCriteria` is inside `Experiment.fingerprint`. A started
experiment refuses a definition whose fingerprint has changed, at the
write (`save_experiment`) and at the run (`run`). Relaxing `min_effect`
after seeing a result produces a *different experiment*, which is
§46 as arithmetic rather than as discipline.

The freeze begins when the experiment **starts**, not when it is
written. A draft nobody has run carries no result that editing it
could flatter.

### 2. The split is chronological, never random

`chronological_split` cuts an already time-ordered cohort at a single
index. A random split of financial observations leaks: two rows from
the same day land on opposite sides, and the held-out half already
knows the answer. Walk-forward protocols delegate to Phase 9's
`WalkForwardSplitter`, purge and embargo included — there is no second
splitter here.

The reported effect is the **out-of-sample** one. The in-sample effect
is kept beside it precisely so the gap between them is visible.

### 3. The cohort is point-in-time safe

`load_cohort` filters on `available_at <= as_of` — Phase 21's key,
which records when an experience became **knowable**, never when the
row was written. That single clause is what stops an experiment
anchored in the past from consulting its own future.

### 4. Nothing production is touched

The engine reads experiences and writes five experiment tables. It
cannot promote a model, change a threshold, alter a strategy, move
capital, or touch the IBKR safety chain.
`tests/experiments/test_engine_and_safety.py` proves it by parsing
every SQL literal passed to `execute` in the package and asserting the
write set is a subset of the experiment tables.

### 5. An evaluator is a registered name

`ArmSpec.evaluator` is a string looked up in a registry. It is never a
callable, never a code string, and the configuration **cannot express
code at all** (§80). A test asserts the package contains no `eval`,
`exec`, `compile`, `__import__` or `import_module` call.

---

## The objects

| Object | What it fixes |
|---|---|
| `Hypothesis` | statement, **mechanism (required)**, expected effect, population, metric, source |
| `ArmSpec` | one side of the comparison: a registered evaluator name, parameters, complexity |
| `DatasetSnapshot` | `as_of`, universe, filters, and four data versions |
| `EvaluationProtocol` | holdout fraction, walk-forward settings, bootstrap iterations, seed |
| `AcceptanceCriteria` | what would count as success — **inside the fingerprint** |
| `ResourceLimits` | max rows, max variants, so one sweep cannot starve the rest |

### Two fingerprints, deliberately

`Experiment.fingerprint` identifies **this experiment**: its
hypothesis text included, because rewording the question asked is a
different experiment once one has started.

`Experiment.comparison_fingerprint` identifies **what is being
measured**: evaluator, parameters, cohort and metric — no names, no
prose. Two experiments with different titles over identical arms are
one piece of evidence with two labels, and
`api.integrity_check` reports how much of the record repeats itself.

Neither fingerprint contains prose. `ArmSpec.identity()` exists
separately from `as_dict()` for exactly this reason: a reworded
description must not invalidate a running experiment, and — the harder
failure — a field with no column behind it makes a stored experiment
hash differently from itself when reloaded.

---

## Evaluators

Seven run today, all over Phase 21 experiences:

`signal_all` · `signal_strength_threshold` ·
`signal_confidence_threshold` · `signal_direction` ·
`signal_event_filter` · `signal_horizon` · `signal_composite`

Six are **declared and unavailable**, and raise an error naming the
tables they need rather than returning an empty cohort:

`regime_filter` · `model_comparison` · `strategy_backtest` ·
`execution_policy` · `portfolio_allocation` · `risk_policy`

An empty cohort would flow through the engine, produce a sample size
of zero, and be reported as INCONCLUSIVE — which reads as *"we tested
and could not tell"* rather than *"we could not test"*. The difference
matters more than the convenience.

### Baselines

`all_signals` · `all_predictions` · `long_only` · `short_only` ·
`coin_flip`

Returned as copies, so a caller cannot mutate the registry and
silently change the control of every later experiment. Inventing a
baseline for one experiment is refused: a control chosen per
experiment is a control chosen to flatter it.

---

## The verdict

Every criterion is checked and every check is recorded, whether it
passed or failed:

- out-of-sample effect against `min_effect`
- sample size against `min_sample` (30, the same threshold Phases 9,
  19, 20 and 21 use)
- bootstrap interval, and whether it excludes zero
- robustness: the fraction of time slices in which the effect held
- complexity ratio: a candidate that adds moving parts must earn them
- economic significance, **signed** — a large effect in the wrong
  direction is reported as a large effect in the wrong direction
- the multiple-testing note, scaled by how many experiments the family
  already contains

`INCONCLUSIVE` is reached before `FAIL` whenever the evidence could
not decide. *"We could not tell"* and *"it does not work"* are
different findings, and collapsing them loses the more useful one.

### Sensitivity has three shapes

- **plateau** — neighbouring parameter values behave similarly; the
  shape a real effect makes
- **single_point** — exactly one value clears the bar and its
  neighbours do not; the shape overfitting makes
- **no_effect** — nothing in the range clears the bar; not a tuning
  problem, because there is no setting at which the candidate wins

---

## Proposals

`templates.propose_from_memory_pattern` and
`propose_from_recurring_error` turn Phase 21 patterns and Phase 20
attributions into **drafts**. Generating is not executing: nothing runs
until a human runs it.

Every mined hypothesis carries the caveat in its own mechanism text:

> This hypothesis was derived from the same historical record it will
> be tested against, so the pattern that suggested it is already inside
> the data.

---

## The pipeline stage

Stage **14/15**, after `build_memory.py` (it mines memory) and after
`attribute_errors.py` (it mines attributions), before the dashboard is
rebuilt.

It runs `--propose --apply` and nothing else. A scheduled job that
both generates hypotheses and decides whether they succeeded, with
nobody in between, is autonomous learning however it is labelled.
`tests/experiments/test_pipeline_position.py` fails if `--run` ever
appears in the workflow.

---

## Files

| File | Lines | What |
|---|---|---|
| `src/domain/experiment_models.py` | 820 | definitions, criteria, fingerprints, statistics |
| `src/data_access/experiment_schema.py` | 272 | five tables, indexes, column migration |
| `src/experiments/evaluators.py` | 420 | registry, seven runnable, six declared unavailable |
| `src/experiments/engine.py` | 945 | cohort, split, run, decide, sensitivity, ablation |
| `src/experiments/templates.py` | 538 | proposals from memory and from errors |
| `src/experiments/api.py` | 566 | the read/act surface, integrity check |
| `scripts/run_experiment.py` | 417 | the CLI |
| tests | 1,388 | 99 tests across four files |

The dashboard adds `_collect_experiments` plus the **Experimente**
workspace and its detail page. The Lab is read-only: the dashboard is
a static file with no server, so where an action is the sensible next
step it shows the command instead.

---

## What this phase does not do

- It does not promote, train, retrain, or adjust any threshold.
- It does not enable live trading, and does not touch the IBKR safety
  chain. Interactive Brokers remains the only broker.
- It implements no reinforcement learning and no autonomous learning.
- It does not read or emit credentials or account information.
- It cannot execute arbitrary code through any interface.
- It does not rank experiments against each other. Ranking by effect
  size *is* the selection bias the phase exists to expose.
