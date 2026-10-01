# Phase 25.9I-F.7 — Historical Corpus Recovery & Research Dataset Restoration

> ## RECOVERED HISTORY CREATES A NEW DATASET VERSION.
> ## IT DOES NOT REWRITE THE PAST EXPERIMENT.

> ## MORE HISTORICAL ROWS ARE USEFUL ONLY IF THEIR TEMPORAL AND PROVENANCE INTEGRITY IS VALID.

> The old database was opened `mode=ro` for the whole phase and its manifest
> hashes are identical before and after. No model was trained, no threshold
> moved, no order placed, no protected outcome read. D20 remains sealed.

Written 2026-10-01. Completion report: `docs/PHASE_25_9I_F7_REPORT.md`.
Pre-registration for the next phase: `docs/PHASE_25_9I_F6B_PREREGISTRATION.md`.

---

## A. Why F.7 exists

Phase 25.9I-F.6 researched four daily horizons and returned **INSUFFICIENT
DATA** on all four. Not because the candidates were weak — because **zero legal
walk-forward folds could be formed**. The research region spanned 15 calendar
days; a single fold needed 16 to 27.

F.6 also located the shape of the problem: canonical events began 2026-07-30
although articles went back to 2026-07-01. That is a 29-day hole at the front of
the corpus, and it is exactly the stretch the research region needed.

F.6 deliberately did not fix it. Two refusals, both recorded:

- it did **not** recompute the holdout boundary after seeing zero folds, because
  choosing a boundary that is known to produce folds is holdout shopping;
- it did **not** rebuild the corpus, because that would have silently changed the
  data underneath Phase 25.9's recorded negative results.

F.7 is the phase that does the upstream work, under an architecture that makes
both of those refusals unnecessary rather than merely honoured: the recovered
history becomes a **new dataset version**, and the old one is not touched at all.

---

## B. The original temporal-history problem, measured

All read-only, from `data/marketlens.db`.

| Stage | Earliest real record |
|---|---|
| article (`articles.published_at`) | **2026-07-01T22:25:59Z** |
| entity link (earliest linked article) | **2026-07-30T13:04:26Z** |
| event report (`events.publication_time`) | **2026-07-30T13:34:32Z** |
| canonical event (`first_reported_at`) | **2026-07-30T13:34:32Z** |
| research observation (`information_cutoff`) | **2026-07-30T13:34:32Z** |

Every downstream stage begins within 30 minutes of the entity-link boundary, and
none of them begins where the articles do. **The corpus did not start late. The
entity stage did.**

---

## C. The exact root cause

### C.1 The funnel, in one unit

Counting **distinct articles reaching each stage** — so the funnel reconciles and
a drop belongs to exactly one stage:

| Stage | Entering | Exiting | Drop | Drop % |
|---|---|---|---|---|
| raw article | 48,392 | 48,392 | 0 | 0.0 |
| normalized article | 48,392 | 48,392 | 0 | 0.0 |
| **entity resolved** | 48,392 | **23,504** | **24,888** | **51.43** |
| event report | 23,504 | 825 | 22,679 | 96.49 |
| fused | 825 | 825 | 0 | 0.0 |
| canonical event | 825 | 825 | 0 | 0.0 |
| research observation | 825 | 825 | 0 | 0.0 |

First material break: **entity resolution**. Fusion, canonicalisation and the
observation builder lose nothing at all.

### C.2 Split on the measured boundary, it becomes unambiguous

**Before 2026-07-30T13:04:26Z**

| Stage | Entering | Exiting | Drop % |
|---|---|---|---|
| raw article | 17,789 | 17,789 | 0.0 |
| normalized article | 17,789 | 17,789 | 0.0 |
| **entity resolved** | 17,789 | **0** | **100.0** |

**From 2026-07-30T13:04:26Z**

| Stage | Entering | Exiting | Drop % |
|---|---|---|---|
| raw article | 30,603 | 30,603 | 0.0 |
| normalized article | 30,603 | 30,603 | 0.0 |
| entity resolved | 30,603 | 23,504 | 23.2 |
| event report | 23,504 | 825 | 96.49 |

Entity resolution loses **23.2%** of what it is given on one side of that instant
and **100%** on the other. A resolver does not behave that way. A date filter
does.

### C.3 What the evidence rules out

| Hypothesis | Refuted by |
|---|---|
| the old articles are a different kind of source | **17,760 of 17,789** pre-cutoff articles come from the same `Google News: <Company>` feeds that get 92% coverage after the cutoff |
| the old articles lack mention data | **15,518 of 17,789** have populated `companies_mentioned`, e.g. `[{"company": "Boeing", "ticker": "BA", ...}]` at 2026-07-02 |
| it is an ingestion cutoff | the articles are present and normalized; `news_articles` holds all 48,392 |
| it is a collection-time effect | in the **2026-08-23 run alone**, 16,248 articles published after the cutoff got 14,696 entity links and **9,767 published before it got 0** — same run, same code, same sources |
| fusion was scoped narrowly | fusion lost **0** reports; `fuse_events.py` has no date argument |
| it is sparse historical coverage | 29 consecutive days with 100–1,400 articles each and **exactly zero** entity links is a step, not a taper |

### C.4 The cause, named

`scripts/backfill_article_entities.py` accepts `--since` and applies it as

```python
if since:
    where.append("published_at >= ?")
```

It was run with that bound set at approximately **2026-07-30T13:04Z**.
**17,789 articles published before it were never scanned.** `populate_events.py`
then processes only `articles ∩ article_entities`, so no event could be extracted
from them, no canonical event formed, and no research observation existed for the
entire first month of the corpus.

`populate_events.py` carries the same `--since` flag. It is a second, redundant
instance of the same hazard, and the recovery passes neither.

### C.5 Loss accounting, reconciled

48,392 source articles, **0 unclassified**:

| | All | Before the cutoff | From the cutoff |
|---|---|---|---|
| became canonical-event evidence | 825 | 0 | 825 |
| legitimate no-event article | 22,662 | 0 | 22,662 |
| **entity resolution never attempted** | **17,789** | **17,789** | **0** |
| entity resolution attempted, unresolved | 7,099 | 0 | 7,099 |
| extraction never attempted | 17 | 0 | 17 |

The pre-cutoff column has exactly **one** reason in it, and that reason is
recoverable by replay. `ENTITY_UNRESOLVED` is explicitly **not** recoverable: the
resolver looked and declined, and loosening it to change that answer is
forbidden — a false mapping is worse than a missing one.

---

## D. Information time is not processing time

The single most important contract in the phase, and the one that decides whether
a recovery is worth doing at all.

A July article replayed in October is a **July information event**. If
`information_time` were allowed to pick up the recovery instant, every purge,
embargo and as-of guarantee downstream would be silently void — and the dataset
would look richer while being contaminated.

`InformationTiming` therefore **refuses** an information time at or after the
recovery instant, and refuses an information time after its own processing time.
Three timestamps are kept, never conflated:

| | Meaning | Where it lives |
|---|---|---|
| **information time** | when the market could know | `articles.published_at` → `events.publication_time` → `canonical_events.first_reported_at` → `research_observations.information_cutoff` |
| **processing time** | when the stage ran originally | `articles.collected_at`, `events.detection_time`, `observation_created_at` |
| **recovery time** | when the replay ran | `recovery_provenance.recovered_at`, with `recovery_run_id` |

`--run` applies the contract to **every** recovered record before the corpus may
be frozen. The measured result: **3,653 records checked, 0 violations.** A single
violation would have marked the run FAILED, and a FAILED run cannot freeze a
corpus or produce a handoff.

Worked example from the recovered corpus:

```
article      published 2026-07-02T07:00:00Z   collected 2026-08-04T12:07:46Z
event report publication_time 2026-07-02T07:00:00Z  detection_time 2026-10-01T17:28:38Z
observation  information_cutoff 2026-07-02T07:00:00Z  created 2026-10-01T17:28:41Z
features     max as_of 2026-07-02T07:00:00Z   (<= cutoff)
labels       d5 measured_at 2026-07-13T04:00:00Z  (> cutoff)
```

The information time is July in all three rows. October appears only where it
belongs.

---

## E. The architecture: a second file, not a rewrite

### E.1 Why a separate database

`build_research_observations.py` derives `observation_id` from
`(event_id, instrument_id)` and then does

```sql
DELETE FROM research_features WHERE observation_id = ?
DELETE FROM research_labels   WHERE observation_id = ?
```

before rewriting them. Pointing it at the existing database would therefore
**overwrite snapshot v1 in place** — the one thing §1 of the brief forbids. No
flag avoids it; the identity is deterministic by design, which is correct for
idempotency and fatal for versioning in the same file.

```
data/marketlens.db              corpus v1 + snapshot v1 + F.6's experiment   READ ONLY
data/marketlens_corpus_v2.db    corpus v2 + snapshot v2                      built here
```

### E.2 Where the registry lives, and why not in the old file

The version registry (`corpus_versions`, `research_snapshots`, `recovery_runs`,
`recovery_provenance`, `cross_version_event_map`) is created **in the new file**,
and the old version is recorded there as a *measurement of* the old database,
with its manifest hashes computed read-only.

That is a stronger guarantee than an entry inside the protected artifact would
have been: anyone can recompute the hash against the untouched original and
compare it with the recorded value. **Nothing had to be written to the protected
artifact in order to prove it was not written to.**

### E.3 What was cleared in the new file, and why

Derived tables are emptied before the replay so the new corpus is a pure function
of the source material that is kept — articles, registries and the price cache —
rather than a mixture of old rows and new. `article_entities`, `events`,
`event_evidence`, the canonical and fusion tables, the event studies, and the
research observation/feature/label tables.

Separately, **v1's experiment output is removed**: `trained_models`,
`model_evaluations`, `model_baseline_comparisons`, `predictions`, `signals`,
`recommendations`. That is not housekeeping. F.6's root cause was a stored
`effective_sample_size` of 4 being read as the corpus's capacity; leaving that row
in the new file would leave the same trap armed. The new database contains **zero**
model evaluations, and a test asserts it.

### E.4 Rollback

The new file is standalone. Deleting it restores the prior state exactly. The
source was never opened for writing, so there is nothing to undo there.

---

## F. The replay reuses every existing stage

No second ingestion, entity, extraction, fusion, study, observation or feature
pipeline exists. `recover_historical_corpus.py` **sequences the existing
scripts** against the new file and records provenance around them:

```
scripts/backfill_article_entities.py --apply      entity resolution
scripts/populate_events.py           --apply      event extraction
scripts/fuse_events.py               --apply      fusion
scripts/build_event_studies.py       --apply      event studies
scripts/build_research_observations.py --apply    observations
scripts/compute_features.py          --apply      features
```

**The only difference from the original run is the absence of `--since`.** A test
asserts that neither `--since` nor `--limit` appears in any stage invocation, so
the root cause cannot be reintroduced by a later edit. A second test asserts the
recovery module imports no resolver, extractor, fusion engine or study engine —
it cannot have quietly reimplemented one.

### Determinism

`populate_events.py` already derives `event_id` from `(article_id, event_type)`
rather than a UUID, which was done for idempotency and pays off here as replay
determinism: the same source evidence produces the same report identity, so
**event IDs are stable across versions** and cross-version matching by fingerprint
is meaningful.

### Idempotency, proven not asserted

Every stage was replayed a second time against the already-built slice:

| | Before | After |
|---|---|---|
| entity links | 587 | **587** |
| event reports | 27 | **27** |
| canonical events | 24 | **24** |
| observations / features / labels | 24 / 768 / 320 | **24 / 768 / 320** |
| corpus manifest hash | `33ab7fd5…` | **`33ab7fd5…`** |
| snapshot manifest hash | `2af2654f…` | **`2af2654f…`** |

Zero new rows, identical hashes. Running the same recovery twice adds nothing but
run metadata.

### No external cost

Every stage script imports no provider, HTTP or LLM module, and every existing
event carries `extraction_tier = deterministic_rule`. The recovery made **0** paid
API calls, **0** LLM calls and **0** network calls, and a test enforces the
absence of those imports. The brief's cost stop never had to fire.

---

## G. Corpus and snapshot versioning

### Manifests

A manifest hash is taken over **sorted identities plus their information-time
fields**, canonicalised as JSON. Row order, insertion order and processing
timestamps are excluded, because those differ between a build and a replay of the
same build and would make the hash useless for the one thing it is for.

| Artifact | Manifest content |
|---|---|
| corpus | each canonical event's id, fingerprint, `first_reported_at`, type |
| snapshot | each observation's id, instrument, cluster, `information_cutoff`, quality |

Neither reads a label value — so a manifest is computable without touching an
outcome, protected or otherwise, and a test checks the string `value_json` never
appears.

### The lifecycle

```
BUILDING -> VALIDATED -> FROZEN -> ELIGIBLE_FOR_RESEARCH
                     \-> REJECTED
```

A corpus or snapshot does **not** become research input because files exist.
`BUILDING` cannot jump to `FROZEN`. `FROZEN` requires a content hash and a freeze
timestamp, or construction raises. Once frozen, exactly **one** change is legal —
`FROZEN → ELIGIBLE_FOR_RESEARCH` with the hash unchanged, because eligibility is
a publishing decision layered on frozen content, not a change to it. Everything
else is refused at both the domain and the persistence layer.

### Recovery runs

Every recovered record points at a `recovery_run_id`, so recovered history is
never indistinguishable from original processing. `mark_complete()` **refuses**
when `processed_through < range_end`, and refuses while any error is recorded. A
partially recovered corpus cannot claim completeness, because nothing downstream
would know to distrust it.

---

## H. Cross-version lineage

Canonical events are matched across versions **by fingerprint**, which identifies
the same underlying event whatever id each build assigned it. Where fingerprints
do not match, the event is reported as `NEW` or `REMOVED` rather than forced into
a correspondence: an improved pipeline is allowed to cluster differently, and
pretending otherwise would hide exactly the difference a later phase needs to
explain.

Measured v1 → v2:

| Relation | Count |
|---|---|
| **SAME** | **579** |
| **NEW** | **474** |
| CHANGED | 1 |
| MERGED / SPLIT / REMOVED | 0 |

579 of v1's 581 events reappear identically. Two differ, one of them reported as
`CHANGED`. Nothing was removed. The 474 new events are the recovered history.

---

## I. The new holdout, and the discipline around it

### The rule is inherited, not invented

```
HOLDOUT_FRACTION = 0.20
boundary = earliest_information_time + 0.80 × span, floored to the UTC day
```

**0.20 is the fraction Phase 25.9 used, carried over unchanged.** That is the
method: the rule predates this dataset, so it cannot have been chosen to suit it.

| | |
|---|---|
| **Boundary** | **2026-08-18T00:00:00Z** |
| Research region | 2026-07-02T07:00Z → 2026-08-18T00:00Z, **46 days** |
| Holdout | 2026-08-18T00:00Z → 2026-08-30T14:35Z, **12 days (21.26%)** |

The old boundary (`2026-08-15T01:23:31Z`) belongs to snapshot v1 and is untouched.

`HoldoutDecision` refuses to construct when `observed_model_outcome` is set, and
refuses a boundary with no stated rule. A boundary chosen after a result cannot be
frozen as a holdout at all.

### Adequacy is a verdict, not a target

| Horizon | Research rows | **Clusters** | Purge | 1 fold needs | 3 folds need | **Max folds** |
|---|---|---|---|---|---|---|
| d1 | 737 | 227 | 3 | 16 | 30 | **5** |
| d3 | 735 | 227 | 5 | 18 | 32 | **5** |
| d5 | 735 | 227 | 7 | 20 | 34 | **4** |
| d10 | 633 | 211 | 14 | 27 | 41 | **3** |
| intraday ×4 | 738 | 227 | 14 | 27 | 41 | **3** |

Verdict: **ADEQUATE**. Every horizon forms at least the required three folds.

Had it not, the answer would have been **DATASET STILL INSUFFICIENT** and
`--freeze` would have exited non-zero without marking anything eligible. It does
not try a different fraction. The same arithmetic that ended F.6 is kept in a test
that reproduces its shortfalls exactly — 15, 17, 19 and 26 days — so the two
phases cannot drift into describing different contracts.

---

## J. Effective sample, recomputed and never inherited

F.6's root cause, stated once more because it is the thing most worth not
repeating: the stored `effective_sample_size` of **4** was the cluster count of a
five-row test window dated 2026-08-29. It was never a property of the corpus.

Two structural defences:

1. `research_snapshot.effective_sample()` counts distinct `event_cluster_id`
   values in the snapshot and **does not reference `model_evaluations` at all** —
   a test parses the module and asserts the name is absent from its code.
2. The v2 database holds **zero** rows in `trained_models`, `model_evaluations`
   and `model_baseline_comparisons`. There is no stale evaluation to misread.

A **cluster is an instrument**, so the effective sample answers *how many distinct
companies is this evidence about*. That definition is v1's and is unchanged. The
limitation it carries — two separate events at one issuer count once — is
unchanged too, and stated rather than fixed.

| | v1 | **v2** |
|---|---|---|
| distinct clusters | 244 | **301** |
| d5 research-region clusters | 178 | **227** |

---

## K. What recovery did **not** improve

Reported here rather than left for the reader to notice.

| | v1 | v2 | |
|---|---|---|---|
| canonical events with `event_time` | 0 of 581 | **0 of 1,056** | **no improvement** |
| `independent_source_count` | 0 everywhere | **0 everywhere** | **no improvement** |
| single-source events | 562 of 581 | **1,036 of 1,056** | slightly worse in share |
| observations marked invalid | 16 (2.8%) | **141 (13.4%)** | **worse** |

The invalid share deserves the sharpest statement: **July observations are 24.5%
invalid against August's 2.8%**, every one for `insufficient_price_data`. More rows
is not the same as more evidence, and the recovered region carries less usable
evidence per row than the region that was already there.

Source independence remains **UNKNOWN** and event occurrence time remains
**absent**. Nothing here infers either. Phase I-F found both and F.7 did not fix
them, because fixing them was not what the funnel pointed at — and inventing
either would have been worse than leaving them.

Entity relationship persistence (also an I-F finding) was **not** touched: it is
not responsible for the corpus loss, so it stays deferred. Scope discipline.

---

## L. Price history was audited and left alone

Prices cover **2025-11-23 → 2026-08-27**, 87,003 rows, 240 instruments, with 8,866
rows in 2026-07. The recovery period is fully covered, so recovered July events can
carry real abnormal-return labels — which is why the recovery was worth running at
all, and was checked **before** it was planned.

The price cache was not rebuilt. This phase is not permission to rebuild every
dataset in the project.

---

## M. Validation before scale

A deterministic 3-day slice was replayed into a throwaway file first: the
**earliest** three days of the never-scanned region, chosen by position in the gap
and never by market outcome. A slice selected for its returns would make the
validation a selection of evidence.

The slice is physically truncated rather than filtered — later articles are
deleted from the throwaway file — because a filter the stage scripts do not have
cannot be trusted, while an absent row can.

Result: 617 articles → 543 entity-resolved (88.0%) → 27 reports → 24 canonical
events → 24 observations → 320 labels. Temporal integrity **0 violations**. Source
hashes unchanged. Status **PARTIAL**, correctly, because a slice does not cover its
range and `RecoveryRun` will not let it claim otherwise.

Lineage was then walked by hand in both directions — raw article → normalized →
entity → report → cluster → canonical event → observation → features and labels —
before the full run was authorised.

---

## N. Protected D20 isolation

| | |
|---|---|
| executed | **NO** |
| inspected | **NO** |
| inferred | **NO** |
| consumed | **NO** |
| used to choose which records to recover | **NO** |
| used to place the new holdout | **NO** |
| used to assess recovery quality | **NO** |
| hypothesis / anchor-v2 / protected window changed | **NO** |

Structural, not intentional:

- `research_snapshot.effective_sample()` **raises** if asked about `d20`.
- `ResearchHandoff` **refuses to construct** unless `d20_excluded` is true.
- `horizon_ids()` excludes it by default; the recovery reports it as `PROTECTED`
  with no counts.
- Boundary tests parse every F.7 module and assert the absence of
  `validate_d20_reversal`, `anchor_v2`, `d20.abnormal_return`, `d20.raw_return`,
  `check_d20_readiness` and `return_60d` — and that no F.7 file restates the
  frozen hypothesis in prose either.

One honest caveat, stated rather than buried: `build_research_observations.py` is
the canonical label builder and it writes **all** horizons' labels, d20 among
them, in the new file as it does in the old. F.7 reused it unchanged, because
forking it would create the second pipeline the brief forbids. What F.7 does is
quarantine the horizon at every layer above the builder: no F.7 code reads a d20
value, no report mentions its counts, and the handoff cannot carry it. The
protected *experiment* — its hypothesis, its anchor-v2 semantics, its window, its
validator and its results in v1 — is untouched.

---

## O. Production boundary

| | |
|---|---|
| broker writes attempted / succeeded | **0 / 0** |
| paper orders / live orders | **0 / 0** |
| Portfolio / Risk / OrderIntent / Execution | **unchanged** |
| capture behaviour / IBKR / subscriptions | **unchanged** |
| production model | **unchanged** |
| qualification gate / thresholds | **unchanged** |

Boundary tests assert no F.7 file imports `src.execution`, `src.brokers`,
`src.paper`, `src.trading`, `src.ibkr`, `ib_insync` or `ibapi`; names no order or
broker symbol in code; writes to no order or execution table; imports no
`ModelingEngine`; and cannot promote or fit a model. One existing file was
modified by this phase — `.gitignore` — because the recovered database is a
196 MB Release asset, not a git file.

---

## P. Scope boundaries

**In scope and done:** diagnose the gap, prove the cause, replay the existing
stages without the publication floor, version the result, preserve lineage and
timing, freeze a new snapshot and a new holdout, and hand off.

**Deliberately out of scope:** model research, model qualification, target and
horizon calibration, production wiring, event-time inference, source-independence
inference, entity-relationship persistence, and any rebuild of the price cache.

**F.6B was not run.** No model of any kind was fitted against snapshot v2. Tests
assert the new database holds zero trained models, zero evaluations, zero
predictions and zero signals, and that the F.7 scripts contain no call to
`train_and_evaluate`, `evaluate_fold`, `ModelingEngine` or `promote_model`.

---

## Q. Handoff to F.6B

`docs/PHASE_25_9I_F6B_HANDOFF.json`, frozen before any candidate was evaluated:

| | |
|---|---|
| snapshot | `research_snapshot_v2`, `ELIGIBLE_FOR_RESEARCH` |
| snapshot hash | `4ccab384d37a9eb249ce2bd61b44692c77a0df3dc7121dc79c3ed6940e66b0bc` |
| corpus | `canonical_corpus_v2`, hash `4247f543904204c9d60f86b9ab6858e3bd3bd9fcc635e5950c5b60e148cb2ddb` |
| pre-registration | `docs/PHASE_25_9I_F6B_PREREGISTRATION.md`, sha256 `9150b988993dcac3…` |
| eligible horizons | **d1, d3, d5, d10** |
| effective sample | 227 / 227 / 227 / 211 |
| holdout | `2026-08-18T00:00:00Z`, 46-day research region |
| purge / embargo | 3, 5, 7, 14 days / 6 days |
| candidate budget | **16 cells, and it will not grow** |
| qualification gate | `ModelEvaluation.is_deployable` |
| D20 | excluded |

Intraday horizons are **data-eligible** — 57 distinct sessions against a floor of
30, where F.6 deferred them at 29 — and are still **deferred on policy**. The only
purge horizon available to them is the 14-day fallback written for d10, and
fourteen calendar days of purge around a five-minute forward window is a
placeholder, not a policy. An intraday purge must be pre-registered on its own
evidence, in a document of its own, and must not be chosen because it produces
more folds. That is precisely why it was not chosen here, under time pressure, in
a document whose other numbers are already known.

---

## R. What this phase does not claim

The recovered dataset makes the question **askable**. It says nothing whatever
about the answer.

F.6's result stands exactly as recorded — **NONE QUALIFIED / INSUFFICIENT DATA**
against the data it actually used — and is not reinterpreted by anything here.
Snapshot v1's manifest hashes are identical before and after this phase, so that
result remains addressable against its own evidence.

> **OLD EXPERIMENTS REMAIN OLD EXPERIMENTS. NEW DATA REQUIRES A NEW EXPERIMENT.**
