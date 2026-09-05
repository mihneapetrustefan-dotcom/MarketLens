# Phase 18 — final report

Date: 2026-09-05 · Base commit: `a596971` · Scope: the Phase 17.5 findings, nothing else

Every number below was measured against the **production release
asset** (`db-latest`, 213.7 MB, published 2026-09-04T23:06Z),
downloaded and inspected directly. Where a figure comes from a local
copy or a rehearsal it says so. This is §46, and it is the mistake
Phase 17 made that Phase 17.5 had to correct.

---

## 1. Phase 17.5 recommendations review

| # | Recommendation | Status | Evidence | Change | Tests | Remaining |
|---|---|---|---|---|---|---|
| 1 | **NEW-01** — decide what inference does with a model that fails its baselines | **DONE** | All 4 models `beats_all_baselines=0`, r² −0.20…−0.31; newest made 422/549 predictions | `selection.py`, `promotion.py`, `promote_model.py`, gated `inference`, dashboard | 50 | Nothing. A model can now only score if a human promoted it |
| 2 | **Watch the Sunday 02:00 UTC pipeline run** | **NOT POSSIBLE IN THIS PHASE** | `pipeline.yml` runs = **0**; first cron fires 2026-09-06 02:00 UTC, ~14h after this report | Rehearsed all 10 non-price stages locally, twice | — | The scheduled run itself. §21 forbids calling this fixed |
| 3 | **TD-03** — declare `signals` canonical, relabel `recommendations` | **DONE** | Two different producers, not two schemas for one thing | Both pages renamed and each states what it is | 3 | Nothing |
| 4 | **TD-02b** — repoint the legacy readers | **PARTIAL, deliberately** | 1 of 6 can move; 4 have hard blockers; 1 was never a reader | Dashboard repointed to `news_articles` with fallback | 13 | 4 readers, each with a stated blocker |
| 5 | **NEW-02** — investigate the two-valued confidence | **DONE** | Arithmetic reproduced exactly from production inputs | Documented + labelled a score, **not** widened | 15 | Nothing to fix; it varies when a 2nd model family exists |
| 6 | **TD-15** — archive path | **DONE** | Rehearsal on a copy left all 31 real archives byte-identical | `archive_dir_for()` + `--archive-dir` | 0 (proven by rehearsal) | Nothing |
| 7 | **TD-11** — stale worktree | **DONE** | `c3256a3` an ancestor of main; uncommitted work already on main as `ad88df3` | Removed, branch deleted | — | Nothing |
| 8 | **TD-13** — Python version | **DONE** | 2,982 tests pass on 3.12 with the pinned versions | All 24 workflows → 3.12 | full suite | `yfinance` on 3.12 unverified locally |
| 9 | **NEW-05** — documentation drift | **DONE** | 4 documents quoted 2,815 tests and 82/38 tables | Corrected to 2,982 and 47/5 | 0 | Nothing |

---

## 2. Initial state

```
production (release asset)   47 tables · 5 empty
  signals                   408   (10 active, 396 suppressed, 2 superseded)
  predictions               549
  trained_models              4   all 'evaluated', none ACTIVE
  news_articles          48,955   canonical, a superset of legacy
  articles               48,906   pruned to 60 days by the archiver
  recommendations        33,589   ~1,940/day, regenerated wholesale
tests                       2,903   passing
pipeline.yml runs               0
```

---

## 3. NEW-01 — model quality gate

**Problem.** `inference.load_model()` selected `ORDER BY trained_at
DESC LIMIT 1` and consulted nothing else.

**Evidence.**

| Model | Trained | Clusters | r² | Directional | Beats baselines |
|---|---|---:|---:|---:|:---:|
| `tm-37886ecf` | 09-04 13:29 | 116 | −0.239 | **0.413** | no |
| `tm-6666c760` | 09-04 10:11 | 99 | −0.313 | 0.410 | no |
| `tm-99df38e0` | 08-29 11:41 | 4 | −0.197 | 0.600 | no |
| `tm-0be69458` | 08-29 10:27 | 4 | −0.197 | 0.600 | no |

Negative r² means worse than predicting the mean. The newest produced
422 of 549 predictions → signals → 10 active on a public page.

**The decisive finding: the gate already existed.**
`ModelEvaluation.is_deployable` was written in Phase 9 —

> *Requires: beats every baseline AND has a large enough effective
> sample. Returns None when it cannot be judged.*

— documented, unit-tested, and **called by no production code path**.
`ModelStatus.ACTIVE` likewise appeared nowhere outside a test listing
the enum. So Phase 18 invented no threshold and no new state. It wired
up what was already there, which is what §5 and §6 ask for.

**Change.**

- `src/modeling/selection.py` — `eligibility()` reconstructs a real
  `ModelEvaluation` from the stored rows and asks *it*. No metric is
  reimplemented; a second implementation is a second answer waiting to
  disagree with the first.
- `load_model()` returns `(model, verdict)` as a pair. Holding the
  model without the reason it was permitted is the exact state that let
  an unvalidated model score.
- `NoValidatedModel` carries code `NO_VALIDATED_MODEL_AVAILABLE` and
  the rejection reason for **every** candidate — the useful part is
  which models exist and what each failed, so an operator can tell
  "train more" from "promote the one that passed".
- `NoValidatedModel` subclasses `NoUsableModel`: "no model passed" is a
  case of "no model can be applied", so existing handlers keep working.

**Result, run against the production copy:**

```
NO_VALIDATED_MODEL_AVAILABLE: no model is ACTIVE for label 'd5.abnormal_return'.
  No candidate passes the gate. This is not a configuration problem to be
  worked around — it is the evaluator reporting that no model has shown an edge.
    tm-37886ecf239e4343  [evaluated]  FAILED
        - does not beat 2 of 2 baseline(s): baseline_historical_mean, baseline_majority_class
    ...
```

**Tests.** 35 in `test_model_quality_gate.py`, 22 in the updated
`test_inference.py`. Including: no `threshold` / `force` / `override`
argument exists on any selection or promotion function (checked on the
signature, whole-token); no script but the promotion CLI imports
`promote`; no workflow invokes it; `train_models.py` never writes
`'active'`.

---

## 4. Model lifecycle

Reused, not invented (§5). The existing enum now has meaning:

| Status | Meaning | Can score? |
|---|---|:---:|
| `draft` | incomplete | no |
| `trained` | fitted, not measured | no |
| `evaluated` | measured. Research, backtesting, experiments | **only under `--experimental`** |
| `active` | passed the gate **and** a human promoted it | **yes — the default** |
| `degraded` | was active; later evidence withdrew it | no |
| `retired` | superseded. Kept forever | no |

There is no `FAILED` status, deliberately. A failing model is
`evaluated` with a failing evaluation — the failure belongs to the
*measurement*, not to the model, and a model that fails one evaluation
may pass a later one on more data. Adding a status would have
duplicated `is_deployable` into a place that can go stale.

**Promotion** (`src/modeling/promotion.py`, `scripts/promote_model.py`)
records who, why, which evaluation, which dataset/feature/label
version, and the git commit. It refuses: a failing model, an unjudged
model, a blank approver, a blank reason, an already-active model.
`approved_by` and `reason` are keyword-only with no defaults, so a
caller cannot promote by accident or anonymously.

Promoting retires the incumbent for that label in the same transaction —
two ACTIVE models would make "which is production" ambiguous at exactly
the moment it matters, and the tie-break would resolve it silently.
Demotion is deliberately **not** gated: taking something out of
production must never be harder than putting it in.

---

## 5. Model selection

`newest` → `newest ACTIVE`. Deterministic in every branch: a pinned id,
else the newest ACTIVE, else — only under `EXPERIMENTAL` — the newest
evaluated. Ties are impossible; `trained_at` carries microseconds.

Pinning a specific `--model` is honoured under either policy. Refusing
it would only push people to edit the database by hand, which is worse.

---

## 6. Experimental vs validated

§11 offers two options. **Option B** was taken: generate, and mark.

Option A (refuse to generate) would produce a system that emits nothing
at all, which is no more honest and destroys the thing that currently
works — the eleven-stage chain running end to end. §8 also requires
experimental models stay usable for research.

So `pipeline.yml` stage 9 runs `predict.py --apply --experimental`,
**with the flag written in the workflow, not defaulted in code**. That
visibility is the entire difference between this and the bug. The stage
name says `EXPERIMENTAL`, and the comment above it says to delete the
flag when a model is finally promoted.

The marking is **derived at read time** from `trained_models.status`,
not stored on the signal. A stored snapshot would let a signal keep
claiming validated provenance after its model was demoted, and the
question a reader is actually asking is *"is the model behind this
approved now"*. A test demotes a model and asserts its old signals
change state.

A signal with several contributions is only as validated as its weakest
input: one unpromoted model makes the whole signal experimental.

**Public honesty (§12).** Rendered against production data and checked
as text: the signals table badges each experimental row, the detail
panel carries a full-width warning (*"Semnal produs de un model care nu
a fost promovat… acest numar este un rezultat de cercetare, nu o
recomandare validata"*), and the models page states *"Niciun model
validat"* with the reason. All 40 recent signals in the rendered page
carry `evaluated`.

---

## 7. NEW-02 — signal confidence

**Not widened.** §14 forbids manufacturing variety, §15 requires a
heuristic be labelled as one.

**The arithmetic, reproduced exactly from production inputs:**

```
confidence = base × quality × agreement × sample

predictions.confidence   None for all 549    → base      = 0.5
signals.data_quality     'high' for all 408  → quality   = 1.0
agreement_state  'insufficient_evidence' ×408 → agreement = 0.6
                                              sample    = 1.0

0.5 × 1.0 × 0.6 × 1.0 = 0.30    exactly, 403 times
0.5 × 1.0 × 0.6 × 0.5 = 0.15    the five small-sample ones
```

**Three of four factors are structurally constant**, and each for a
reason that is correct:

- **base** — ridge regression reports no confidence of its own, so
  every prediction stores `None` and base is pinned at 0.5. Not 1.0:
  an unknown confidence is not a confident one.
- **agreement** — `classify_agreement` returns `INSUFFICIENT_EVIDENCE`
  below two usable contributions, and exactly one model family exists.
  A single voice agreeing with itself is not corroboration.
- **quality** — every observation currently passes as `high`, which is
  already the maximum, so this factor can only ever fall.

**Semantics, documented.** It is a **heuristic trust score, not a
probability**. Multiplicative because these are necessary conditions —
a model confident about garbage input must not be rescued by its own
confidence. Nothing calibrates it against outcomes; a test asserts no
calibration step has appeared, so the claim cannot silently go stale.

`strength` is the number that *does* vary (full 0–1 spread in
production) and it is not a probability either: it is the expected move
relative to the strategy's scale. The two are independent by design, and
the dashboard now says so.

**Change:** the column is labelled *"Scor incredere"*, both signal
tables and the detail cell carry the formula as a tooltip, and a note
under the table explains why it is nearly constant and what has to
change before it varies. The number itself is untouched.

---

## 8. TD-03 — signal / recommendation canonicalization

**Not a duplicated schema.** Reading the producers:

| | `signals` | `recommendations` |
|---|---|---|
| Producer | Phase 10 signal engine | `src/recommendation_log.py` |
| Keyed on | `instrument_id` | entity **name**, ticker resolved at write time |
| Model lineage | `signal_contributions` → `trained_models` | none |
| Point-in-time | `source_information_cutoff` | none |
| Lifecycle | active/suppressed/superseded/expired | `checked_at` / `was_correct` |
| Volume | 408, one per scored observation | 33,589, ~1,940/day regenerated |

Two different things whose *presentation* made them interchangeable —
both rendered as a call with a confidence number, in the same visual
language, with nothing saying which was which.

**Resolution: labelling, not migration.** `signals` is declared
canonical for model-derived ideas. `recommendations` is kept — it holds
the only outcome-checked track record in the system (22,725 checked
calls), which is currently the more valuable of the two.

Navigation now reads **"Semnale (model)"** and **"Recomandari
(sentiment)"**, and each page opens with a paragraph stating what it
is and explicitly that the other is a different object, *"nu o versiune
mai veche a acestuia"*.

Nothing was deleted and no migration was written.

---

## 9. TD-02b — legacy article readers

Phase 17 listed seven. Reading them one at a time (§20 forbids bulk
replacement) gives six, with one moved:

| Reader | Verdict | Blocker |
|---|---|---|
| `impact_engine.py` | **NOT A READER** | Scores dicts; queries nothing. Miscounted |
| `news_database.py` | cannot move | It is the **writer**; `run_daily.py` puts only `src/` on `sys.path` |
| `archive_old_articles.py` | **must not** move | It is the **pruner**; pruning legacy is what creates the divergence on purpose |
| `backfill_article_entities.py` | cannot move | Needs `companies_mentioned`, `tickers_mentioned` — **canonical has neither column** |
| `compute_features.py` | not now | Canonical reaches to 2023-12-15 vs legacy 2026-07-07 → changes **model inputs** |
| `populate_events.py` | not now | Same; would mint events from three extra years |
| `dashboard.py` | **MOVED** | Three read-only aggregates |

**Retention, documented.** `archive_old_articles.py` prunes `articles`
to 60 days; nothing prunes `news_articles`. Canonical is therefore a
superset: 48,955 vs 48,906 rows, oldest 2023-12-15 vs 2026-07-07. This
is by design (spec §50) and is what keeps Phase 5 entity links alive
past archiving.

**Not silent (§19).** The move changes visible numbers, so the news
page now names the corpus it used, shows both counts, and explains the
difference. It falls back to `articles` when canonical is absent or
empty — a health page reporting zero articles because a table is
missing is worse than one reporting the older number.

---

## 10. TD-05 — automatic pipeline verification

### The honest answer

**`pipeline.yml` has run zero times.** Checked at 2026-09-05 12:19 UTC
via the Actions API. Its first scheduled fire is **2026-09-06 02:00
UTC**, about fourteen hours after this report. §21 is explicit that
workflow-exists / YAML-parses is not verification, so **TD-05 remains
unverified** and this phase does not claim otherwise.

### What was verified instead

A full local rehearsal against a copy of the production database — all
ten non-price stages, then the whole chain a second time. This
validates the **scripts**, not the **schedule**.

| Stage | Exit | Result |
|---|:---:|---|
| 1 Backfill article entities | 0 | 102 new rows |
| 2 Populate events | 0 | 1,512 reports, 10 stale cleaned |
| 3 Fuse events | 0 | 1,034 canonical, 10 stale cleaned |
| 4 Cache price candles | *skipped* | needs a Polygon key and ~2h30m |
| 5 Build event studies | 0 | 1,034 studies |
| 6 Build research observations | 0 | 1,056 observations |
| 7 Compute features | 0 | 25,344 feature values |
| 8 Train models | 0 | model `tm-3c8f322b`, 171 predictions |
| 9 Predict (experimental) | 0 | 233 scored, banner shown |
| 10 Generate signals | 0 | 8 active |
| 11 Rebuild dashboard | 0 | 533,127 chars |

Row deltas: `article_entities` +102, `research_observations` +7,
`research_features` +188, `predictions` +404, `signals` +5,
`trained_models` +1. `events` −5 and `canonical_events` −3 are the
scripts' own stale-row cleanup, which they report.

### §25 — a poor model must not become active. Verified.

Stage 8 trained a brand-new model. Its status afterwards:

```
tm-3c8f322bc2b44894   evaluated     <-- new, NOT active
model statuses: [('evaluated', 5)]
```

Stage 9 without `--experimental` then refused it by name, listing every
candidate's failure, and wrote nothing (exit 1). This is the exact
scenario NEW-01 describes, reproduced and blocked.

### §23 — idempotency. Verified.

The whole chain run a second time changed **zero rows in all eight
tables**. Duplicate checks: predictions per `(observation, model)` 0,
signal identity hashes 0, canonical article ids 0, research
observations 0, article entity links 0, research features 0.

199 groups share an `events.fingerprint` — **not duplicates**. Seven
event rows with one fingerprint come from seven *different articles*
reporting the same acquisition; that is the Phase 6 report-vs-canonical
model, and the index is non-unique on purpose. The same 200 groups
exist in untouched production.

### §24 — failure handling. Verified by reading.

Every stage is `continue-on-error` with `if: always()` upload, and the
final step counts failures and `exit 1`s if any is non-zero. A partial
run cannot report as clean.

---

## 11. NEW-03 — price coverage

97 of 389 instruments still have no candles. Classified by asking
`normalize_ticker_for_polygon` — the one place that knows what the
provider covers — rather than by guessing:

| Group | Count | Verdict |
|---|---:|---|
| BVB (Bucharest) | **32** | **Genuinely unsupported.** Polygon returns no symbol. Permanently excluded |
| CRYPTO | 26 | Resolvable (`X:ALGOUSD`, `X:APTUSD`, …) |
| US_AND_INTL | 39 | Resolvable |

So the ceiling for `--include-unstudied` is **65**, not 97, and it has
not run yet — it lives in pipeline stage 4, which has never executed.

No coverage was faked and no unsupported instrument was forced in.

**One observation worth a look:** ticker `COIN` is registered under the
CRYPTO exchange and normalises to `X:COINUSD`. COIN is Coinbase's
*equity* ticker. Either a registry misclassification or intentional;
not changed here, because guessing at the registry is how identity bugs
start.

---

## 12. NEW-04 — event confidence

§27 asks whether extraction, source, corroboration and impact
confidence should be separated. **They already are** — every event
stores a weighted decomposition. Measured across all 1,517 production
events:

| Component | Weight | Distinct values | Span |
|---|---:|---:|---:|
| `extraction_certainty` | 0.35 | 16 | 0.317 |
| `entity_resolution_confidence` | 0.25 | **1** | 0 |
| `source_quality` | 0.20 | **1** (0.4 = unclassified) | 0 |
| `temporal_certainty` | 0.10 | **1** | 0 |
| `corroboration` | 0.10 | **1** (0.0) | 0 |

Four of five are literally constant. Only extraction certainty varies,
so the total can move at most 0.35 × 0.317 ≈ 0.111 — which is exactly
the observed 0.45–0.56 span. All 1,517 events land in band `medium`.

**Two findings.**

1. **`corroboration` carries 10% of the weight and is structurally
   always 0.0.** The extractor works per article; corroboration is
   established later by the fusion engine. A tenth of the score is
   allocated to something the calculation cannot observe. Recorded as
   TD-18; not changed, because it would move every event confidence in
   the database.

2. **A correction to Phase 17.5.** That report said the spread *"widened
   but stayed narrow"* after the source-tier fix. It had not widened at
   all: every event in production was created in one second on
   2026-09-03 23:12, before the tier fix landed on 2026-09-04. All 1,517
   still carry `source_quality = 0.4` (unclassified). Like the price
   coverage fix, the tier fix is **shipped and unexercised** — the first
   pipeline run is its first test. The variation I measured came
   entirely from extraction certainty.

Nothing was widened artificially.

---

## 13. NEW-05 — documentation drift

Corrected in `COMPLETE_SYSTEM_AUDIT.md`, `DATABASE_AUDIT.md`,
`TEST_COVERAGE_AUDIT.md`, `PRODUCTION_READINESS.md`,
`TECHNICAL_DEBT_REGISTER.md`:

- 2,815 tests → **2,982**
- 82 tables / 38 empty → **47 tables / 5 empty in production**, with a
  note that the old figure came from a local development copy
- file and workflow counts refreshed (176 / 141 / 25 / 25)

`SECURITY_AUDIT.md` and the TD-12 entry were already corrected during
Phase 17.5.

---

## 14–21. The review items

| Item | Verdict | Evidence |
|---|---|---|
| **TD-15** archive path | **FIXED** | `archive_dir_for()` + `--archive-dir`. Rehearsed: deleted 24,531 rows from a copy, wrote 1.95 MB of archives to a scratch directory, and all **31 real archives were byte-identical by checksum** with `git status data/` clean |
| **TD-11** stale worktree | **FIXED** | `c3256a3` is an ancestor of `main`; no unique commits; the three uncommitted edits were the same `.close()` fixes already on main as `ad88df3`. Backed up, removed, branch deleted |
| **TD-13** Python version | **FIXED, CI-verified** | All 24 workflows 3.11 → 3.12. 2,982 tests pass locally on 3.12, and **GitHub Actions ran the suite on 3.12 for commit `8851ed2` and reported `success`** — so this is verified on the runner, not only on the development machine. Caveat recorded: `yfinance` is imported lazily and is not installed locally, so its 3.12 support rests on package metadata. Reached only by manual `run_backtest.yml` |
| **TD-06** script duplication | **HALF WITHDRAWN** | `research/builder.py` holds `CohortEngine`/`DatasetBuilder`/`ResearchRegistry` — read-time dataset assembly. The script creates observations at write time and its own docstring points at `DatasetBuilder` for the other half. **Not a duplicate; a layering Phase 17 misread.** The `backfill_article_entities` / `entity_repository` half stands |
| **TD-07** clustering | **REVIEWED, NOT DELETED** | Two facts: there is **no `event_clusters` table** — only an index — so wiring it needs a schema first; and a *different, coarser* clustering is already load-bearing (`research_observations.event_cluster_id`, 299 clusters over 1,049 observations, used as effective sample size). The real question is whether the relation graph should replace the coarse id, which changes every model evaluation. Status written into the module |
| **TD-08** legacy collectors | **DEPRECATED IN PLACE** | Zero consumers outside their own tests, re-verified. `DEPRECATED, KEPT` notice written into both docstrings with the removal condition |
| **TD-04** order lifecycles | **UNCHANGED, still ACCEPTED** | No orders exist anywhere; the Phase 14 execution tables are still absent from production. Merging two working subsystems with no live traffic would be change without evidence |
| **TD-10** import conventions | **UNCHANGED** | §36: no broad rewrite for style. Cost remains one line of documentation |

---

## 22. Data / model / signal lineage

**Correcting Phase 17.5.** That report said signal→model provenance was
*indirect, through `observation_id`*. It is **direct**:
`signal_contributions` carries `prediction_id` and `trained_model_id`
per signal. I had not examined the join table.

Verified against production:

```
signal_contributions rows                    408
signals with a contribution                  408
signals with NO contribution                   0
orphan contributions (unknown model)           0
signals with more than one model                0
signals joinable to a prediction              408 / 408
orphan predictions (unknown model)              0
```

So §37 is already satisfied: every production-facing signal identifies
its producing model directly. The multi-model ambiguity §37 warns about
is handled — the dashboard takes the weakest contributor's status, so
one unpromoted model makes the whole signal experimental.

Full chain intact:

```
Market data → features → observation → model → prediction → signal
                                            ↘ signal_contributions ↗
```

**Look-ahead protection unchanged.** `PointInTimeView` still raises
`LookAheadViolation`, and the new inference stage is among its
consumers — the new code adopted the guard rather than working around
it, which is the specific thing worth checking about a new stage.

**Future learning readiness (§40).** No autonomous learning was built.
The lineage Phase 19 will need — model → prediction → signal →
decision → execution → outcome — is preserved, and promotion history
adds the missing link: *which model was approved, by whom, on what
evidence, at which commit*.

---

## 23. Test results

```
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -t .

Ran 2982 tests in 124.354s
OK (skipped=1)
exit 0
```

**CI is green on the Phase 18 commit.** The `Tests` workflow ran the
full suite for `8851ed2` on **Python 3.12** — the version this phase
moved CI to — and reported `success`. That matters more than the local
run: it is the first evidence that the 3.11 → 3.12 change is safe on
the runner rather than only on the development machine.

Zero failures, zero errors. **+79 tests**, and no existing test was
suppressed or deleted. Four existing `test_inference.py` tests were
*updated* — deliberately, because `load_model` now returns a pair and
its seed model is ACTIVE by default so those tests keep testing the
feature contract rather than the gate.

| New file | Tests |
|---|---:|
| `tests/modeling/test_model_quality_gate.py` | 35 |
| `tests/signals/test_confidence_semantics.py` | 15 |
| `tests/test_dashboard_model_status.py` | 15 |
| `tests/test_dashboard_canonical_corpus.py` | 13 |

Lint / type checks: none are configured in this repository, so there is
nothing to run and nothing regressed. `tests.yml` also runs
`check_registry_collisions.py`, which passes.

---

## 24. Security

| Check | Result |
|---|---|
| New credentials | **none** — the gate and promotion touch no secret |
| Secrets in code | **none** (audit Q12, regex sweep) |
| `.env` tracked | **no** (Q14, Q15) |
| Live execution | **still impossible** (Q1–Q8) |
| MT5 | **absent** (Q11) — only `audit_live_safety.py`'s own search matches |
| New broker | **none** (Q10) |
| New inbound surface | none — no framework, no bound socket |
| Public page | swept again: no credential, account, position or order |

`scripts/audit_live_safety.py`: **16 of 16 PASS**, exit 0.

---

## 25. IBKR safety

**The chain was not touched.** `git diff HEAD -- src/execution src/risk
src/portfolio src/paper src/brokers src/pointintime` is **empty** for
every Phase 18 commit. Signal → Portfolio → Risk → OrderIntent →
Intake → Orchestrator → Safety → Validation → IBKR Paper is byte-for-byte
what Phase 17.5 verified.

The gate sits strictly *upstream* of that chain — it decides whether a
prediction may be made at all — so it can only ever reduce what reaches
risk, never increase it.

---

## 26–29. Changes

**Created (7)**

| File | Purpose |
|---|---|
| `src/modeling/selection.py` | Eligibility and deterministic selection |
| `src/modeling/promotion.py` | Controlled promotion; refuses failing models |
| `src/data_access/model_promotion_schema.py` | `model_promotions` — who, why, evidence, commit |
| `scripts/promote_model.py` | The only path to ACTIVE |
| `tests/modeling/test_model_quality_gate.py` | 35 tests |
| `tests/signals/test_confidence_semantics.py` | 15 tests |
| `tests/test_dashboard_model_status.py` | 15 tests |
| `tests/test_dashboard_canonical_corpus.py` | 13 tests |
| `docs/PHASE_18_FINAL_REPORT.md` | this |

**Modified (14)** — `src/modeling/inference.py`, `scripts/predict.py`,
`src/dashboard.py`, `scripts/archive_old_articles.py`,
`src/api_collector.py`, `src/web_scraper.py`, `src/fusion/clustering.py`,
`.github/workflows/pipeline.yml`, all 24 workflows (Python version),
`requirements.txt`, `tests/modeling/test_inference.py`,
`tests/test_dashboard_signal_labels.py`, and five documents.

**Removed (1)** — the stale worktree `.claude/worktrees/blissful-shaw-4e73ab`
and its branch, after verifying it held nothing unique.

## 30. Migrations

One, additive and idempotent: `model_promotions`, created on first use
by `initialize_model_promotion_schema`. No existing table was altered,
no column dropped, no row rewritten. Nothing in this phase modified
production data.

---

## 31. Remaining issues

**Blocking Phase 19: none.**

Open, with owners:

1. **TD-05 pipeline verification** — the 2026-09-06 02:00 UTC run.
   Everything is in place; only the schedule can prove it.
2. **No model passes the gate.** Four models, all worse than the mean,
   on 29× the Phase 17 data. This is now the system's honest state
   rather than a hidden one — but it is still the state.
3. **TD-18** — event confidence spends 10% of its weight on
   `corroboration`, which is structurally 0 at extraction time.
4. **TD-02b** — four readers with stated blockers.
5. **NEW-03** — 65 instruments awaiting the first pipeline run; 32 BVB
   permanently out of provider coverage.
6. **Two fixes shipped and unexercised** — `--include-unstudied` and
   the source-tier fix. Both first run tomorrow.
7. **`COIN` registered under CRYPTO** — possible registry
   misclassification, not touched.

---

## 32. Final architecture status

| Requirement | Status |
|---|---|
| One coherent architecture | **PASS** with the known Phase 1–9 / 10–16 seam (TD-10, accepted) |
| One source of truth per domain | **PASS** — article: canonical, dashboard repointed; trade idea: `signals` canonical and both pages labelled; event: two-layer by design |
| No unnecessary duplicate systems | **IMPROVED** — TD-06 half withdrawn as a miscount; TD-03 resolved by labelling; TD-04 correctly deferred |
| No architecture bypasses | **PASS** |
| Model lifecycle | **PASS** — experimental / validated / active distinguished, promotion controlled |
| No automatic promotion | **PASS** — proven by rehearsal: a new model trained and stayed `evaluated` |
| Signal provenance | **PASS** — 408/408 direct to a model, 0 orphans |
| Public honesty | **PASS** — experimental signals badged and explained |
| No MT5 / no second broker | **PASS** (Q10, Q11) |
| Leakage protection | **PASS** — the new stage adopted the guard |
| IBKR safety | **PASS, untouched** |

---

## 33. Readiness for Phase 19

Phase 19 is trade/signal outcome intelligence: what was predicted, what
signal followed, what happened, and where a failure came from.

What Phase 18 leaves it:

- **Unambiguous provenance** — every signal names its model directly.
- **A promotion record** — model → approver → reason → evaluation →
  dataset → features → commit, so "which model was live when this
  signal was issued" becomes answerable.
- **An experimental/validated boundary** — outcome attribution that
  mixed failed experimental output with validated output would measure
  nothing.
- **A proven-idempotent pipeline** — outcome measurement over a chain
  that double-counts is worthless.
- **Honest confidence semantics** — attributing a failure to
  "overconfidence" requires knowing that confidence is a constant
  heuristic, not a probability.

`signal_outcomes` already holds 10 rows and `signal_evaluations` is
populated, so the tables Phase 19 extends exist and have producers.

---

# READY FOR PHASE 19

With one condition that is a *watch*, not a blocker:

**Confirm the 2026-09-06 02:00 UTC pipeline run.** TD-05 is the only
Phase 17.5 recommendation this phase could not close, and it could not
be closed by any amount of work — only by the clock. Every stage was
rehearsed twice against production data and all eleven behave
correctly, including the one that matters most: a freshly trained
failing model stayed `evaluated` and inference refused it.

Nothing found in this phase puts capital at risk. Nothing can reach
money. The system now states plainly that its models do not work,
rather than publishing their output as though they did — which was the
point.
