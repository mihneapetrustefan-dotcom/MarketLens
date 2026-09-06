# PHASE 22 — FINAL REPORT

**Experiment Engine & Hypothesis-Driven Research**
Date: 2026-09-06 · Method version `v1`

---

## PHASE 21 RECOMMENDATIONS REVIEW

Phase 21 closed with five open items and one standing watch. Their
status now:

**1. Zero validated experience.** Unchanged, and now visible in a
second place: every experiment run in this phase drew on experimental
experience, because no model has earned promotion. The Lab does not
launder that into production evidence.

**2. 27 days of history.** Unchanged — 28 days now. This is the
binding constraint on Phase 22, not a background note. A chronological
split of 28 days leaves a held-out half measured in days, which is why
every one of the six experiments returned an interval that includes
zero. Time is still the only fix.

**3. Regime memory is empty** (`market_regime` NULL throughout).
Unchanged, and it is why `regime_filter` is registered as unavailable
rather than quietly returning empty cohorts.

**4. Execution, portfolio and risk memory are defined and empty.**
Unchanged. The same six gaps appear here as six declared-but-unrunnable
evaluators, each raising an error that names the tables it needs.

**5. Memory confidence is uncalibrated.** Unchanged as a property, but
Phase 22 is the apparatus that could change it: a pattern can now be
put to a chronologically-split test and told whether it survives. One
was. It did not — see §12.

**The standing watch.** Phase 21 noted that its memory stage had not
yet run on a schedule. That is now moot in the way that matters: the
pipeline gained another stage, so §41 records what has and has not run
under automation.

---

## 1. Executive summary

Phase 22 built the apparatus for testing hypotheses against the
system's own record, and then used it. Five experiments were proposed
from memory patterns and recurring errors, and one more was written by
hand. **All six were run and all six FAILED.** Every one of the six
produced a bootstrap interval that includes zero. The one derived from a memory pattern — a cohort
memory records at a 45% hit rate over 363 experiences, quality
*confirmed*, confidence *high* — scored **−6.69% out of sample**
against a required +2.00%, with an in-sample effect of −0.32%.

The phase's most useful output is not a result but a measurement of a
failure mode. The hand-written experiment — *does a 0.7 strength floor
improve directional accuracy?* — scored an in-sample effect of
**+10.96%** and an out-of-sample effect of **−2.13%**, a gap of
**+13.09%**. The engine names that gap in the verdict text as *"the
signature of a candidate that fitted its training half"*.

Three of the five proposals turned out to measure an identical
comparison under three different names, returning byte-identical
numbers. That is reported, counted, and now detectable
— see §14.

Three defects were found and fixed along the way, one of them severe:
**the dashboard had been rendering a blank page since Phase 20**
(§16).

---

## 2. Initial state

Phase 21 left 6,510 experiences, 610 patterns (415 of them WEAK),
7,574 error attributions, and no way to ask whether any of it predicts
anything. There was no experiment table, no notion of a control, and
no place to write down what would count as success.

---

## 3. What was built

Five tables (`hypothesis_families`, `experiments`, `experiment_runs`,
`experiment_results`, `experiment_artifacts`), a domain layer of 820
lines, an engine of 945, an evaluator registry, a proposal generator, a
read API, a CLI, a dashboard workspace with a detail page, and 99
tests. Full architecture in
[`PHASE_22_EXPERIMENT_ENGINE.md`](PHASE_22_EXPERIMENT_ENGINE.md).

---

## 4. Hypotheses require a mechanism

`Hypothesis.validate()` refuses a hypothesis with no proposed
mechanism. A statement without one is a data-mining result wearing a
hypothesis costume, and the validator is the only thing between the
two.

---

## 5–7. Arms, controls, and one changed variable

An experiment has exactly two arms. `changed_variables` is **computed**
from the two `ArmSpec`s rather than declared, so an experiment cannot
claim to change one thing while changing three — the most common way a
result becomes uninterpretable.

Baselines come from a registry of five and are returned as copies. A
baseline invented for one experiment is a baseline chosen to flatter
it.

---

## 8. Acceptance criteria are fixed before running

`AcceptanceCriteria` sits inside `Experiment.fingerprint`. A started
experiment refuses a changed fingerprint at both the write and the run.
Moving the goal posts produces a different experiment; §46 is
arithmetic here, not discipline.

The freeze begins at **start**, not at writing. A draft nobody has run
carries no result that editing it could flatter, and
`test_a_draft_may_still_be_edited` pins that distinction.

---

## 9–11. Splitting, point-in-time safety, and reuse

The split is chronological. `load_cohort` filters on `available_at`,
Phase 21's knowability key. Walk-forward protocols delegate to Phase
9's `WalkForwardSplitter` with its purge and embargo; Phase 12's
`BacktestEngine` is the executor for strategy experiments. There is no
second splitter and no second backtester in this package.

This is the fourth phase in a row where the correct move was to call
infrastructure that already existed rather than build a parallel
version of it.

---

## 12. The memory-derived experiment failed

The hypothesis: the cohort `event_type=acquisition,
expected_direction=long` is directionally more accurate than all
signals. Memory rated the pattern **confirmed**, confidence **high**,
stability **stable**, over 363 experiences.

| | |
|---|---|
| effect out of sample | **−0.0669** |
| effect in sample | −0.0032 |
| required | +0.0200 |
| 95% bootstrap interval | [−0.2122, +0.0807] — includes zero |
| robustness | held in 1 of 3 slices (33%), required 60% |
| verdict | **FAIL** |

This is the phase working. A confirmed, high-confidence, stable
pattern over 363 experiences did not survive a chronological split.
Nothing in Phase 21 was wrong: it reported a co-occurrence and said so.
Phase 22 asked the next question and got an answer.

---

## 13. All six experiments, as run

| Experiment | in-sample | out-of-sample | gap | interval | verdict |
|---|---|---|---|---|---|
| memory pattern: acquisition + long | −0.0032 | **−0.0669** | +0.0637 | [−0.2122, +0.0807] | FAIL |
| response to recurring timing_error | +0.0032 | +0.0049 | −0.0017 | [−0.0005, +0.0104] | FAIL |
| response to recurring magnitude_error | −0.0075 | +0.0145 | −0.0220 | [−0.0781, +0.1108] | FAIL |
| response to recurring prediction_error | −0.0075 | +0.0145 | −0.0220 | [−0.0781, +0.1108] | FAIL |
| response to recurring signal_error | −0.0075 | +0.0145 | −0.0220 | [−0.0781, +0.1108] | FAIL |
| strength floor ≥ 0.7 (hand-written) | +0.1096 | **−0.0213** | +0.1309 | [−0.1585, +0.1113] | FAIL |

Six of six FAIL. Six of six intervals include zero.

The `timing_error` interval, [−0.0005, +0.0104], is the closest thing
to a signal in the table and it still contains zero. Reported as
"+0.49% improvement" it would look like a finding; reported with its
interval it is what it is.

---

## 14. Three of those rows are the same experiment

The three `recurring *_error` rows are byte-identical because they
*are* identical:
the same evaluator (`signal_strength_threshold`), the same parameters
(`threshold = 0.5`), the same cohort, the same metric. Only the names
and the hypothesis text differ, because the proposal generator built
one per error type.

Read as three findings they would treble the apparent evidence. The
per-family multiple-testing correction does **not** catch them,
because they sit in three different families.

So `Experiment.comparison_fingerprint` was added — keyed on what is
measured, never on what it is called — and both
`api.integrity_check` and the Lab report the count. On this database:
**2 of 6 experiments repeat a comparison another experiment already
makes.**

This is a real weakness in the proposal generator, left in place and
made visible rather than papered over. Fixing the generator to
deduplicate is Phase 23 work; measuring the problem is Phase 22 work.

---

## 15. Overfitting is measured, not warned about

The strength-floor experiment is the clearest case: in-sample
**+0.1096**, out-of-sample **−0.0213**, gap **+0.1309**, interval
[−0.1585, +0.1113]. The verdict text names the gap as the signature of
a candidate that fitted its training half.

It is also the one that would have looked best if only the in-sample
number were reported. A +11% improvement in directional accuracy is
the kind of result that gets deployed. It held in 2 of 3 time slices
and cleared the complexity ceiling — the robustness checks *passed* —
and it is still worthless, because out of sample the effect is
negative and the interval contains zero.

The gap is a first-class column in the Lab, not a footnote in a detail
page, because it is the number most likely to be flattering and least
likely to be looked for.

---

## 16. A defect found: the dashboard had been blank since Phase 20

The sidebar is built at module scope from expressions like
`D.attribution.available`. Phases 20 and 21 each added such an entry
but wired their collector into the **operations** payload rather than
the top-level one. The missing key threw a `TypeError` while the
sidebar array was being constructed, so the IIFE never finished,
`MLGo` was never defined, and **the entire terminal rendered as a
blank page** — every workspace, not only the new ones.

Nothing caught it. The collectors had tests. The SQL had tests. The
generator produced a 593 KB file without complaining. The failure
existed only in a browser, and the page had not been opened in one.

Verified against `HEAD` (`71c64b8`) before fixing, so this is a
report of a shipped defect and not of a mistake made this session.

Fixed, and `tests/test_dashboard_experiments.py` now parses the
sidebar out of the generated JavaScript and asserts every payload key
it dereferences exists. It is deliberately generic: it will catch the
next phase that adds a nav entry and forgets the payload.

---

## 17. A second defect: two views named the same function

`viewOutcomes()` was defined twice in one scope. JavaScript keeps the
last, so Phase 19's "Rezultate reale" silently replaced the legacy
sentiment track record, and two sidebar entries both routed to
`#outcomes`. The legacy page was unreachable dead code.

Renamed to `viewRecommendations()` with its own route. Both pages are
reachable again.

---

## 18. Helpers no longer hide schema mistakes

`_rows` and `_scalar` swallowed every `sqlite3.OperationalError` into
an empty result. A missing table is expected and should degrade
gracefully; a missing **column** is a bug, and absorbing both looks
identical from the outside — an empty section.

That cost real debugging time three separate times, most recently when
the Lab listing came back empty because the query named two columns
that do not exist. Both helpers now swallow only *"no such table"* and
raise everything else. The full dashboard was rebuilt against the
production-derived database afterwards to confirm no latent column
bugs were being hidden elsewhere: it builds clean in 17.5s.

---

## 19. A third defect: every stored experiment was unrunnable

`ArmSpec.description` was inside the fingerprint but had no column
behind it. A saved experiment reloaded with an empty description and
recomputed to a **different fingerprint**, so `run()` refused it as
edited. The propose-then-run path — the one a user actually takes —
was broken end to end, while a propose-and-run-in-one-process path
worked, which is why it was not noticed sooner.

Two fixes, both principled rather than expedient: prose left the
fingerprint (`ArmSpec.identity()`), and the descriptions gained columns
plus a migration so they are stored rather than silently dropped.

`test_fingerprint_survives_a_save_and_load` is the regression test.

---

## 20–24. Statistics

Bootstrap intervals are percentile intervals on the difference of
means, seeded and deterministic — a research number that changes when
you look at it twice is not a research number. Below 30 observations
per arm the interval is `None` rather than wide: an interval computed
on eight observations is an invitation to read eight observations as
evidence.

Economic significance is **signed**. An early version reported a
−2.13% effect as clearing the 1% economic threshold, which read as a
point in the candidate's favour; it now returns `False` with *"large
enough to matter and in the wrong direction"*.

The multiple-testing note scales with the family. A family with one
experiment is a pre-registered test; a family with fifty is a search.

---

## 25–27. Sensitivity, ablation, robustness

Sensitivity sweeps a parameter and classifies the surface as
**plateau**, **single_point**, or **no_effect**. The third was added
after a sweep in which zero of seven values cleared the threshold was
reported as *"only one parameter value clears"* — zero working and one
working are different findings, and "single point" implies an optimum
exists to tune toward.

Ablation removes one component at a time. Robustness counts the time
slices in which the effect held, and the fraction appears in the
verdict text.

---

## 28–30. Resource limits

`max_rows` and `max_variants` are enforced by refusal, never by silent
truncation. A truncated cohort answers a different question than the
one asked and the reader cannot tell.

---

## 31–33. Evaluators

Seven runnable, six declared unavailable. The unavailable six raise an
error naming the tables they need. An empty cohort would flow through
the engine, produce a sample size of zero, and surface as
INCONCLUSIVE — *"we tested and could not tell"* rather than *"we could
not test"*.

The six correspond exactly to the six empty memory layers Phase 21
reported. The gap is the same gap, stated in a second place.

---

## 34–36. Dashboard

The **Experimente** workspace leads with the denominator: defined,
drafts, carried to a verdict, passed, failed, inconclusive. A proposal
is not evidence, and a page that counted drafts alongside results would
report research that was never done.

The detail page shows hypothesis, mechanism, why it exists, both arms,
computed changed variables, the predefined criteria, the three effect
numbers, the interval, every reason, every limitation, robustness, and
the fingerprint.

The Lab is read-only. The dashboard is a static file with no server
behind it, and §80 forbids arbitrary execution through a public
interface — so where an action is the sensible next step, the page
shows the exact command.

---

## 37–38. CLI

`scripts/run_experiment.py` with `--propose`, `--run`, `--sweep`,
`--ablate`, `--families`, `--compare`, `--templates`, `--evaluators`.
Nothing writes without `--apply`.

---

## 39–40. Proposals are drafts

Every proposal is written as `DRAFT`. Generating is not executing, and
`test_proposals_are_not_written_unless_asked` pins that proposing does
not write at all without `--apply`.

Every mined hypothesis carries the caveat inside its own mechanism
text: it was derived from the same record it will be tested against.

---

## 41. The pipeline

Stage **14/15**, after memory and attribution, before the dashboard.
It runs `--propose --apply` and nothing else — a scheduled job that
both generates hypotheses and decides whether they succeeded is
autonomous learning however it is labelled.
`test_the_pipeline_proposes_and_does_not_run` fails if `--run` ever
appears in the workflow.

**Not yet verified under automation.** The stage was added in this
commit and the next scheduled run is Wednesday 02:00 UTC. This is
carried forward as the one open verification item, in the same terms
Phase 21 used for its own stage.

---

## 42. Testing

99 new tests, all passing:

| File | Tests |
|---|---|
| `tests/experiments/test_experiment_models.py` | 31 |
| `tests/experiments/test_engine_and_safety.py` | 44 |
| `tests/experiments/test_pipeline_position.py` | 8 |
| `tests/test_dashboard_experiments.py` | 16 |
| **total** | **99** |

Full suite: **3,441 tests, OK, 1 skipped**, 178s.

One test was rewritten after passing for the wrong reason: the fixture
used `direction_result` values of `"correct"`/`"wrong"` where
production uses Phase 19's `"hit"`/`"miss"`, so every arm had zero
decided outcomes and every experiment came back INCONCLUSIVE for a
reason that had nothing to do with the code under test.

---

## 43. Safety

Enforced by parsing this package's own source, not by convention:

- every write targets an experiment table; no production table is
  written
- no module imports `src.modeling.promotion`, `src.execution`,
  `src.risk`, or `src.portfolio.rebalance`
- nothing promotes, trains, fits, places an order or sets capital
- no `eval`, `exec`, `compile`, `__import__`, `import_module` or
  `os.system`
- no reinforcement learning, no autonomous learning, no self-update
- no LLM
- no credentials, API keys, or account identifiers
- no broker other than Interactive Brokers is referenced anywhere
- the workflow contains no `--live` and no order placement

---

## 44. Versioning

`EXPERIMENT_METHOD_VERSION = "v1"` is part of the experiments primary
data and every page pins one version. Methodology versions coexist by
design; a page that did not pin would add two methodologies over the
same experiments and report twice the research that was done.

---

## 45. Performance

Cohort load and split over 6,510 experiences: under a second. A full
run including bootstrap and three robustness slices: ~2s. Five
experiments end to end: ~11s. Dashboard build against the
production-derived database: 17.5s.

---

## 46. Remaining issues

1. **The proposal generator produces duplicates.** Three of five
   proposals were one comparison. Detected and counted; not yet
   prevented.
2. **28 days of history.** Every interval in §13 includes zero, and a
   chronological split of 28 days is the reason. Time is the only fix.
3. **Six evaluators cannot run** because their source tables do not
   exist — the same six gaps Phase 21 reported.
4. **Zero validated experience.** No model is promoted, so every
   experiment ran on experimental evidence.
5. **The pipeline stage has not yet run under automation.** Next
   scheduled run Wednesday 02:00 UTC.
6. **No experiment has passed.** Zero of six. The Lab reports a pass
   rate of zero rather than finding something to celebrate.

---

## 47. Next phase

Phase 23 inherits an apparatus that can state a hypothesis with a
mechanism, fix success before seeing the answer, split by time, and
report an out-of-sample effect with an interval and every reason
behind the verdict — plus a record that admits when it is repeating
itself.

The obvious first work: deduplicate proposals at generation, and let
the record accumulate enough time that a held-out half is worth
holding out.

---

# READY FOR PHASE 23

Phase 22 built the apparatus and used it on the system's own record.
Six experiments were run and **six failed**, every one with an interval
that includes zero — including one derived from a pattern memory rated
confirmed, high-confidence and stable over 363 experiences.

It measured overfitting rather than warning about it: +10.96% in
sample, −2.13% out of sample, a gap of +13.09%, named in the verdict.
It counted the two experiments in its own record that repeat a
comparison already made. It found and fixed three defects, including a
dashboard that had been rendering a blank page since Phase 20 and a
fingerprint bug that made every stored experiment unrunnable.

It changes no model, strategy, threshold, feature, sizing, risk limit,
execution setting or capital figure. Interactive Brokers remains the
only broker, live trading remains disabled, and promotion remains a
human decision.

The engine works. It has not yet found anything, and it says so.
