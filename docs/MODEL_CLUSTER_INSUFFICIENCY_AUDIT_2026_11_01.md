# Diagnostic Audit — Model Training Cluster Insufficiency

**Date:** 2026-11-01
**Repository:** `mihneapetrustefan-dotcom/MarketLens`
**Trigger:** run #18 trained `ridge_abnormal_return:v1` with `insufficient_data: true`, empty
coefficients, abstention rate 1.0 and `beats_all_baselines = SQL NULL`
(see `docs/POST_SUNDAY_PIPELINE_VERIFICATION_2026_11_01.md`).

**FINAL STATUS: CAUSE IDENTIFIED**

Strictly read-only. No source, config or test file was edited. No cluster construction was
changed. No observation was created or modified. No training, evaluation, pipeline script or
workflow was run. The production database, the `db-latest` release asset and all three backups
in `Documents\MarketLensBackups` were **not opened**. No provider was contacted. D20 untouched.
No model was promoted or qualified. No gate was relaxed. Nothing was committed or pushed. The
only write was this report.

---

## 1. Objective and scope

The question posed was *why 835 observations yield only 79 independent clusters*. The audit
answers a corrected version of that question, because the premise does not survive inspection,
and then answers the question that actually matters: **why the model could not be fitted.**

Two findings reframe the task, and both are verified facts:

1. **The "79 clusters" figure is not a count of training clusters.** `trained_models.
   train_cluster_count` is populated with the **test**-fold cluster count. The training-fold
   cluster count is computed, printed, and then discarded — it is never passed to the engine and
   never persisted. So the production record pairs 835 *training rows* with 79 *test* clusters.
   The two numbers have different denominators and were never comparable.

2. **The fit failure has nothing to do with cluster count.** `fit_ridge` sets
   `insufficient_data: True` on exactly one condition — fewer than two **complete-case rows**.
   Cluster count never enters that decision; it only triggers an advisory note. The real cause is
   that the 835-row training matrix contained **fewer than 2 rows with no missing feature**.

The cause of *that* is established in §6 and is an upstream data-retention problem, not a
statistical or clustering one.

---

## 2. Data provenance and freshness

Per the restrictions, production was not touched. Three sources were used.

### 2.1 Primary: committed repository state

Source code at `origin/main` (`1b6e216`), read with `git ls-tree` + `git cat-file`. **Current and
authoritative** — the code that ran in run #18 is byte-identical to this.

### 2.2 Production *outputs*, from a committed file — not from the database

`docs/index.html` at `1b6e216` is the dashboard **produced by run #18** and committed to `main`.
It embeds that run's own aggregates as a JSON object. Reading it is reading a repository file, not
querying production.

This is the only source of Nov 1 production figures in this report, and it is genuine production
data: `health.size_bytes` in it equals the `db-latest` asset size byte-for-byte. Its limits are
stated where they bite — most importantly, `feature_coverage` is filtered to
`source='phase8_feature_engine'` and `value_json != 'null'` (`src/dashboard.py:232-236`), so it
shows the Phase-8 feature slice only, not the Phase-6 features that also enter the design matrix.

### 2.3 Local databases — both STALE, used only for structure

| File | Size | mtime (UTC) | What it is |
|---|---|---|---|
| `data/marketlens.db` | 186.9 MiB | **2026-09-11T08:21:54Z** | gitignored local snapshot, same lineage as production (48,392 `news_articles` vs production's 62,567) |
| `data/marketlens_corpus_v2.db` | 196.3 MiB | **2026-10-04T16:55:38Z** | the recovered historical research corpus — a separate artifact, **not** a production snapshot |

Both opened with `file:…?mode=ro` and inspected under `python -I`.

**Neither can represent the November 1 production dataset, and neither is treated as if it
could.** Stated plainly:

- `data/marketlens.db` is **51 days stale**. It holds 581 research observations against
  production's 1,295, and its newest evaluation is 2026-08-29.
- `data/marketlens_corpus_v2.db` is **28 days stale**, has no `trained_models` or
  `model_evaluations` rows at all, and is a recovery artifact rather than the live corpus.

They are used for two legitimate purposes only: (a) to establish the **structure** of clustering
and the design matrix, which is set by code that has not changed; and (b) as a **before** picture
against which the Nov 1 production coverage figures can be compared. Every Nov 1 number in this
report comes from §2.2, never from these files.

### 2.4 What cannot be established

- The exact number of **training** clusters on Nov 1. It was printed to the run log, and the log
  is inaccessible (HTTP 403 unauthenticated; no `gh`; no token with `actions:read`).
- The exact complete-case row count in the Nov 1 training fold. Known only to be **< 2**, by
  deduction from `insufficient_data: True`.
- Whether Phase-6 (`phase6_event_study`) features were still present on Nov 1, since the
  dashboard's coverage view excludes them.

---

## 3. Full cluster-construction path

Traced end to end in the committed source.

### 3.1 Observation construction

`scripts/build_research_observations.py` builds one `ResearchObservation` per
(event, instrument) pair and persists it with `INSERT OR REPLACE` (line 257). **There is no
`DELETE FROM research_observations` anywhere in the codebase** — the observation table only ever
accumulates. This matters a great deal in §6.

### 3.2 Cluster-key generation — one line

```python
# scripts/build_research_observations.py:251
event_cluster_id=instrument_id,
```

The cluster key **is the instrument id**, assigned at the write site with no transformation. Not
derived, not hashed, not bucketed.

This is corroborated by the module that exists to police it,
`src/adaptive/sample_adequacy.py:17-22`:

> `research_observations.event_cluster_id` is the clustering key, and audited 2026-09-30 its value
> **is the instrument id**: 244 distinct clusters over 244 distinct instruments, zero nulls.

And measured again here, independently, on both local databases:

| | distinct `event_cluster_id` | distinct `instrument_id` | rows where they differ | NULL cluster ids |
|---|---|---|---|---|
| `marketlens.db` (Sep 11) | 244 | 244 | **0** | 0 |
| `corpus_v2.db` (Oct 4) | 301 | 301 | **0** | 0 |

### 3.3 Grouping and deduplication

There is no deduplication step. Grouping is implicit: `load_dataset` carries `cluster_ids`
alongside each row and the caller counts distinct values
(`scripts/train_models.py:336-337`).

```python
train_clusters = len({cluster_ids[i] for i in train_idx})
test_clusters  = len({cluster_ids[i] for i in test_idx})
print(f"Clustere (instrumente) : {train_clusters} antrenare / {test_clusters} testare")
```

`train_clusters` is **printed and then never used again.**

### 3.4 Training sample selection

`scripts/train_models.py:115-175`:

1. All `research_observations` with a non-NULL `information_cutoff`, ordered by cutoff ascending.
2. Rows with `quality_level = 'invalid'` dropped (line 123) — counted and reported, not hidden.
3. Numeric features only; booleans and categoricals excluded rather than integer-encoded
   (lines 146-147).
4. **`feature_names = sorted(numeric_names)` (line 163) — the UNION of every numeric feature
   name seen on any observation.**
5. Each row is assembled as `[by_observation[oid].get(name) for name in feature_names]`
   (line 168) — **`.get` yields `None` for every feature that observation lacks.**
6. Rows with no resolved label are skipped (lines 166-167).
7. `cluster_ids.append(cluster_id or observation_id)` (line 172) — a NULL cluster id would
   degrade to a per-row singleton. Not exercised: zero NULLs measured.

Steps 4 and 5 are the mechanism that later destroys the matrix. A feature present on 9 % of
observations becomes a column that is `None` on the other 91 %.

### 3.5 Chronological train/test split

`chronological_split` (lines 178-208), with `TRAIN_FRACTION = 0.7` and `EMBARGO_DAYS = 6.0`:

- boundary = the cutoff of row `int(n * 0.7)` in cutoff order;
- `cutoff < boundary` → train;
- `boundary <= cutoff <= boundary + 6 days` → **embargoed, dropped from both sets**;
- otherwise → test.

Time-ordered with an embargo at least as long as the d5 label horizon. There is no leakage here,
and the split is not the problem.

### 3.6 Effective sample-size calculation — where the mislabel enters

```python
# scripts/train_models.py:382-385
model, evaluation = engine.train_and_evaluate(
    specification, X_train, Y_train, X_test, Y_test,
    feature_names=feature_names, window=window, cluster_count=test_clusters,
)
```

**One** `cluster_count` argument is accepted, and it is given `test_clusters`. Inside
`src/modeling/engine.py`:

```python
# line 259 — in train()
train_sample_size=len(X), train_cluster_count=cluster_count,
# line 346 — in evaluate()
cluster_count=cluster_count,
```

So the single test-fold number is written to **three** fields:
`trained_models.train_cluster_count`, `model_evaluations.cluster_count`, and
`model_evaluations.effective_sample_size`. That is why the Nov 1 record shows `cluster_count = 79`
and `effective_sample_size = 79` — they are the same number, and it is the test count.

**Verified by exact reproduction.** Replaying §3.4-3.5 against the Sep-11 snapshot:

| | reproduced here | persisted in that DB's `trained_models` |
|---|---|---|
| train rows | **320** | `train_sample_size = 320` |
| **test** clusters | **4** | `train_cluster_count = 4` |
| **train** clusters | **171** | *not stored anywhere* |

320 and 4 match the persisted 2026-08-29 row exactly, while the real training-fold cluster count
was **171**. The field is mislabeled, not merely coarse.

For Nov 1 the same arithmetic applies: **835 training rows, 79 test clusters.** The training
cluster count is unknown; it is bounded above by the 389 instruments in the registry, and by the
Sep-11 ratio (171 clusters from 320 rows) a value in the low hundreds is *inferred*, not measured.

### 3.7 Minimum sample sufficiency checks

Three distinct gates, and it is worth separating them because only the first fired:

| Gate | Location | Condition | Consequence | Fired on Nov 1? |
|---|---|---|---|---|
| **Fit sufficiency** | `src/modeling/algorithms.py:116` | `len(clean_X) < 2` after listwise deletion | `insufficient_data: True`, empty coefficients, abstain on everything | **YES** |
| Cluster advisory | `src/modeling/engine.py:267-269` | `cluster_count and len(X) >= cluster_count * 3` | appends a note; changes nothing | YES (835 ≥ 237) |
| Qualification floor | `src/modeling/engine.py:391-394` | `effective_sample_size < MIN_EFFECTIVE_SAMPLE` | marks the evaluation descriptive-only | not determinable without the log |

The decisive gate is the first, and it does not consult clusters at all:

```python
# src/modeling/algorithms.py:115-118
clean_X, clean_Y = _clean_rows(X, Y)
if len(clean_X) < 2:
    return {"family": ..., "coefficients": [], "intercept": 0.0, "insufficient_data": True}
```

```python
# src/modeling/algorithms.py:36-51
def _clean_rows(X, Y):
    """Drop rows containing a missing feature or label.
    Missing values are NOT imputed here..."""
    for row, label in zip(X, Y):
        if label is None or any(v is None for v in row):
            continue
```

**Complete-case (listwise) deletion over the union of all feature columns.** The no-imputation
policy is deliberate and correct — `MissingPolicy` exists precisely so that "missing" and "zero"
stay different facts. But combined with a union-of-columns matrix it means **one sparse column
can delete the entire dataset.**

The advisory note the production record carries,
`"835 rows from only 79 clusters — effective sample is far smaller than it appears"`, is generated
at `engine.py:268-269` as `f"{len(X)} rows from only {cluster_count} clusters"` where `len(X)` is
the **training** row count and `cluster_count` is the **test** cluster count. The sentence that
prompted this audit compares two different folds.

---

## 4. Statistical distribution of observations across clusters

From the two local databases. **Structure, not current magnitudes** — see §2.3.

### 4.1 Cluster size distribution

| | `marketlens.db` (Sep 11) | `corpus_v2.db` (Oct 4) |
|---|---|---|
| observations | 581 | 1,056 |
| clusters (= instruments) | 244 | 301 |
| **mean** obs/cluster | **2.38** | **3.51** |
| **median** | **2.0** | **3** |
| min / max | 1 / 15 | 1 / 19 |
| singleton clusters | 102 (41.8 % of clusters) | 58 (19.3 % of clusters) |
| 10 largest clusters | 13.1 % of observations | 10.9 % of observations |
| top 10 % of clusters | 25.1 % of observations | 25.4 % of observations |

Size histogram (Sep 11): `1:102, 2:49, 3:43, 4:24, 5:16, 6:6, 7:2, 11:1, 15:1`
Largest clusters (Sep 11): `us_and_intl-blk` 15, `crypto-btc` 11, `us_and_intl-meta` 7,
`us_and_intl-uber` 7, then five at 6.

**This distribution is healthy, and it is not the problem.** A mean of 2.4-3.5 observations per
instrument with a long thin tail is close to the best case for event-study data: no cluster
dominates, the top decile holds a quarter of the rows, and more than a fifth to two fifths of
clusters are singletons. Concentration is mild.

### 4.2 Instruments, entities and event families

| | Sep 11 | Oct 4 | **Nov 1 (production)** |
|---|---|---|---|
| research observations | 581 | 1,056 | **1,295** |
| distinct instruments / clusters | 244 | 301 | not published |
| instruments in registry | — | — | **389** |
| canonical events | 581 | 1,056 | **129** |
| event families (`event_type`) | — | — | **27** |
| corroboration | — | — | single_source 116 / multi_source 13 |

Event families on Nov 1 are reasonably spread: litigation 28, product_launch 14, partnership 10,
inflation 9, earnings 8, acquisition 7, then a tail of 21 types with 1-6 each.

The row that matters is `canonical events`. In both historical snapshots it equals the
observation count **exactly** (581 = 581, 1,056 = 1,056), and `observations per event_id` is
`(1, 581)` — precisely one observation per event. On Nov 1 that 1:1 relation is broken:
**1,295 observations against 129 canonical events.**

### 4.3 Time coverage

| | Sep 11 | Oct 4 |
|---|---|---|
| cutoff range | 2026-07-30 → 2026-08-27 | 2026-07-02 → 2026-08-30 |
| distinct cutoff dates | 29 | 58 |
| by month | 2026-07: 37, 2026-08: 544 | 2026-07: 514, 2026-08: 542 |
| distinct (cluster, date) pairs | 520 of 581 rows | 962 of 1,056 rows |

Sep 11 shows real temporal concentration (94 % of observations in one month) but Oct 4 does not
(even split across two). On Nov 1 the corpus spans at least 2026-07 to 2026-10 — longer again.
**Temporal concentration is improving, not worsening**, so it does not explain a failure that is
new.

### 4.4 Design matrix, replayed exactly as `load_dataset` builds it

| | Sep 11 | Oct 4 |
|---|---|---|
| usable observations (quality ≠ invalid, cutoff set) | 565 | 915 |
| rows entering X (d5 label resolved) | 458 | 805 |
| clusters in X | 223 | 234 |
| **complete-case rows (survive `_clean_rows`)** | **449 (98.03 %)** | **795 (98.76 %)** |
| clusters among complete-case rows | 219 | 234 |
| missing-features-per-row | 0 → 449 rows; 1 → 9 rows | 0 → 795; 1 → 10 |
| worst column | `sentiment.dispersion_7d`, missing in 2.0 % | `sentiment.dispersion_7d`, missing in 1.2 % |
| split (0.7 / 6-day embargo) | train 320 / test 5 / embargoed 133 | train 561 / test 122 / embargoed 122 |
| train clusters / test clusters | 171 / 4 | 202 / 99 |
| **train rows surviving `_clean_rows`** | **312 → fit_ridge FITS** | **552 → fit_ridge FITS** |

On both historical datasets the matrix is 98 %+ complete and the ridge **fits**. The failure mode
seen on Nov 1 **is not reproducible on either**, which is itself the strongest evidence that what
changed is the data, not the code — and no code in this path changed
(`git log --since=2026-10-06 -- scripts/compute_features.py src/modeling/ src/research/
scripts/build_research_observations.py` returns **nothing**).

---

## 5. Evidence of duplication and dependence

### 5.1 Genuine independent observations versus repeated measurements

Three nested notions of independence, measured:

| Level | Sep 11 | Oct 4 | What it means |
|---|---|---|---|
| rows | 581 | 1,056 | every row is a distinct (event, instrument) pair |
| **events** | 581 | 1,056 | **one observation per event** — no event is measured twice |
| **clusters (instruments)** | **244** | **301** | the honest denominator |
| (cluster, date) pairs | 520 | 962 | same-day repeats within a cluster are rare |

So there is **no row-level duplication at all**: `observations per event_id` is uniformly 1, and
observation ids are deterministic (`observation_id_for(event_id, instrument_id)`), so a re-run
replaces rather than accumulates.

The dependence that exists is the real and unavoidable kind: **2.4-3.5 observations of the same
instrument**, from different events at different times. Fifteen BlackRock observations across one
month are fifteen views of one company in one regime. The codebase already treats this correctly
— clustering by instrument is exactly the right conservative choice, and
`sample_adequacy.py:45-51` explicitly refuses to count duplicated rows, resampled clusters or
within-cluster folds as independent evidence.

### 5.2 Missing or unresolved entity/event relationships — the real defect

This is where duplication analysis gives way to the actual finding.

| Relationship | Sep 11 | Oct 4 | **Nov 1** |
|---|---|---|---|
| observations | 581 | 1,056 | **1,295** |
| canonical events available to join to | 581 | 1,056 | **129** |
| **observations whose event still resolves** | 581 (100 %) | 1,056 (100 %) | **~124 (9.6 %)** |
| live `articles` rows | 48,392 | 48,392 | **12,964** |
| `article_entities` links | 26,400 | 44,597 | 48,685 |

**Roughly 1,170 of 1,295 observations (90 %) now point at events and articles that are no longer
in the live tables.** The observations themselves are intact; their evidence is not.

---

## 6. Root cause

**Verified fact.** A seven-step chain, every step read in the committed source and corroborated by
production output.

### Step 1 — Articles older than 60 days leave the live table, daily

```yaml
# .github/workflows/daily.yml:126-127
- name: Archive old articles (preserves history, shrinks live DB)
  run: python scripts/archive_old_articles.py --keep-days 60
```

The script's own default is **180** days; the workflow overrides it to **60**. It moves rows to
gzipped monthly JSONL and then `DELETE FROM articles WHERE …`
(`archive_old_articles.py:192`). Live `articles`: 48,392 → **12,964**.

The script's docstring (lines 8-11) names this exact risk:

> **WHY ARCHIVE INSTEAD OF DELETE:** this project's Phase 7 (Historical Event Studies & Research
> Dataset) needs exactly the old articles this script would otherwise remove. Silently deleting
> them … would quietly sabotage that later goal.

The archive does preserve them — `data/archives/` holds **35 files, 24 MB, spanning 2023-10 to
2026-09**. But **nothing in the codebase reads `data/archives/` back.** A grep for `archives/`
across `scripts/` and `src/`, excluding the writer, returns **zero** hits. The protection is
nominal: history is kept in a form no downstream stage consults.

*(`reduce_database_size.py --keep-days 14` is **not** implicated. It prunes only `raw_articles`
and its docstring explicitly excludes `articles`. Verified.)*

### Step 2 — Event reports are derived from the live table, and stale ones are deleted

`scripts/populate_events.py:115` reads `FROM articles a` — the **live** table only. Then
lines 250-263:

```python
current_ids = {e.event_id for e in reports}
existing_ids = {r[0] for r in conn.execute("SELECT event_id FROM events")}
stale = existing_ids - current_ids
for stale_id in stale:
    for table in ("events", "event_participants", "event_evidence", ...):
        conn.execute(f"DELETE FROM {table} WHERE event_id = ?", (stale_id,))
```

An event this run cannot re-derive — because its source articles were archived — is **deleted**.
`events`: 846 → **153**.

### Step 3 — Canonical events are rebuilt from those reports, with the same sweep

`scripts/fuse_events.py:176-182` does the identical thing one layer up:

```python
existing = {r[0] for r in conn.execute("SELECT canonical_event_id FROM canonical_events")}
stale = existing - current_ids
for stale_id in stale:
    for table in ("canonical_events", "canonical_event_participants", ...):
        conn.execute(f"DELETE FROM {table} WHERE canonical_event_id = ?", (stale_id,))
```

`canonical_events`: 581 → **129**.

### Step 4 — Research observations never leave

`INSERT OR REPLACE`, no `DELETE` anywhere (§3.1). The corpus climbs to **1,295** while the event
table it was built from falls to **129**.

### Step 5 — Feature recomputation LEFT JOINs the survivors

```sql
-- scripts/compute_features.py:228-235
SELECT ro.observation_id, ro.instrument_id, ro.information_cutoff, ro.event_type,
       ce.quality_confidence, ce.independent_source_count
FROM research_observations ro
LEFT JOIN canonical_events ce ON ce.canonical_event_id = ro.event_id
```

A **LEFT JOIN**: when the canonical event is gone, `ce.quality_confidence` and
`ce.independent_source_count` come back `NULL`, silently, and flow straight into
`metadata["event_confidence"]` / `metadata["independent_source_count"]` (lines 274-278).
Article-derived features fail the same way — `load_entity_articles` (line 160) also reads
`FROM articles a`, the live table.

### Step 6 — Feature coverage collapses, and *which* features collapse is the proof

Same metric on both sides (`COUNT(DISTINCT observation_id)` where the value is non-null,
`source='phase8_feature_engine'`):

| Phase-8 feature | Sep 11 (581 obs) | **Nov 1 (1,295 obs)** | |
|---|---|---|---|
| `news.article_count_7d` | 581 — 100 % | **1,295 — 100 %** | survives |
| `event.count_7d` | 581 — 100 % | **1,295 — 100 %** | survives |
| `event.count_30d` | 581 — 100 % | **1,295 — 100 %** | survives |
| `volatility.realized_5d` | 566 — 97.4 % | 1,257 — 97.1 % | survives |
| `market.return_1d` / `_5d` | 566 — 97.4 % | 1,257 — 97.1 % | survives |
| `market.drawdown_60d` | 566 — 97.4 % | 1,257 — 97.1 % | survives |
| `technical.rsi_14` | 561 — 96.6 % | 1,250 — 96.5 % | survives |
| `volatility.realized_20d` | 561 — 96.6 % | 1,250 — 96.5 % | survives |
| `market.return_20d`, `market.distance_from_ma20` | 561 — 96.6 % | 1,250 — 96.5 % | survives |
| `liquidity.relative_volume_20d`, `liquidity.volume_zscore_20d` | 561 — 96.6 % | 1,250 — 96.5 % | survives |
| `volatility.percentile_20d`, `market.return_60d` | 556-560 — ~96 % | 1,247 — 96.3 % | survives |
| **`sentiment.mean_7d`** | 581 — 100 % | **133 — 10.3 %** | **COLLAPSED** |
| **`news.source_diversity_7d`** | 581 — 100 % | **133 — 10.3 %** | **COLLAPSED** |
| **`event.days_since_last`** | 581 — 100 % | **133 — 10.3 %** | **COLLAPSED** |
| **`event.novelty_365d`** | 581 — 100 % | **124 — 9.6 %** | **COLLAPSED** |
| **`event.independent_source_count`** | 581 — 100 % | **124 — 9.6 %** | **COLLAPSED** |
| **`event.confidence`** | 581 — 100 % | **124 — 9.6 %** | **COLLAPSED** |
| **`sentiment.dispersion_7d`** | 572 — 98.5 % | **111 — 8.6 %** | **COLLAPSED** |

The split is not random, and that is what makes this conclusive rather than suggestive:

- **Everything derived from price candles survived at ~96-97 %.** Candles are cached in
  `price_candle_cache` (162,341 rows) and are not archived.
- **Everything derived from articles or event metadata collapsed to ~9-10 %** — matching the
  129 surviving canonical events (129 / 1,295 = 10.0 %) almost exactly.
- **Counts survived; statistics did not.** `event.count_7d`, `event.count_30d` and
  `news.article_count_7d` stayed at 100 % because a count over an empty window is legitimately
  **0** — a real number. A *mean*, a *dispersion*, a *confidence* or a *days-since* over an empty
  set is undefined, so it is correctly written as `null`. The feature engine is behaving exactly
  as designed; it is being asked about evidence that is no longer there.
- `sentiment.dispersion_7d` is the rarest at **111**, below `sentiment.mean_7d`'s 133, because a
  standard deviation needs two articles where a mean needs one.

### Step 7 — The design matrix amplifies 9 % sparsity into total failure

The union of numeric columns includes all seven collapsed features. A row survives `_clean_rows`
only if **every** column is present. Therefore:

> **complete-case rows ≤ 111** — the rarest column's coverage — **out of 1,295 (8.6 %).**

The 70 % earliest-cutoff training fold then takes 835 rows from that pool. The surviving complete
rows are concentrated in the recent window (that is *why* they survive — their articles and events
are still live), and the training fold is the **oldest** 70 %. Fewer than two complete rows landed
in it, so:

```
len(clean_X) < 2  →  insufficient_data: True, coefficients: []
                  →  predict_batch returns None for every row
                  →  197 predictions, 197 abstentions, abstention_rate 1.0
                  →  model_score NULL on both baseline comparisons
                  →  beats_all_baselines = None  →  persisted as SQL NULL
```

Every link is a verified fact except the bolded inference below.

**Inference (not measured):** that the ≤111 complete rows fall predominantly in the test tail
rather than the training fold. It follows from the mechanism — completeness requires live articles
and events, which only recent observations have — and it is consistent with the outcome, but the
row-level distribution is in the inaccessible log.

### 6.1 Ranked causes against the candidate list

| Candidate | Verdict | Evidence |
|---|---|---|
| **Incomplete data ingestion / retention** | **PRIMARY CAUSE — verified** | 60-day article archiving + two stale-sweep deletions vs an append-only observation table; coverage collapse to 9-10 % confined to article/event-derived features |
| **Clustering rules** | **NOT a cause** | cluster count never enters the fit decision; distribution is healthy (mean 2.4-3.5, top decile 25 %) |
| **Entity mapping gaps** | Not a cause | `article_entities` **grew** 26,400 → 48,685; `linked_articles` 42,947 of 62,567 |
| **Duplicate observations** | Not a cause | exactly one observation per event in both snapshots; deterministic ids |
| **Temporal concentration** | Not a cause | coverage **widened** (29 → 58 → 120+ days) |
| **Insufficient event coverage** | **Symptom, not cause** | 129 canonical events is a consequence of steps 1-3, not an input shortage: 1,056 existed on Oct 4 |
| Design-matrix construction (union + listwise deletion) | **Contributing amplifier — verified** | `algorithms.py:36-51` and `train_models.py:163-168`; turns 8.6 % sparsity into zero usable rows |
| Field mislabeling (`train_cluster_count`) | **Reporting defect — verified** | `engine.py:259`; reproduced exactly (320/4 vs true 171) |

---

## 7. Upstream data improvements

Ranked by impact, with implementation risk. **None of these manufactures independence**; each
either restores evidence that already existed or stops discarding it.

| # | Change | Impact | Risk | Why it does not fabricate independence |
|---|---|---|---|---|
| **1** | **Stop deleting derived events whose source articles were archived.** Make the stale sweeps in `populate_events.py:259-263` and `fuse_events.py:178-182` distinguish "no longer corroborated" from "source article no longer live". | **Highest.** Would restore the observation→event join for ~90 % of the corpus, and with it all seven collapsed features. | **Medium.** The sweeps exist for a stated reason (orphan rows from the uuid4 id migration). The fix must preserve that while sparing rows whose only sin is age. | Restores real events that were already observed; invents nothing. |
| **2** | **Read `data/archives/` back when computing features**, or raise `--keep-days` for the window Phase 7/8 needs. 35 files, 24 MB, 2023-10 → 2026-09 are already on disk, already complete. | **Highest.** Directly addresses the docstring's own warning. Cheapest in data terms — the evidence is already saved. | **Low-Medium.** A loader for gzipped JSONL is self-contained; the live-DB size constraint that motivated 60 days is untouched if the archive is read lazily rather than reloaded. | Reads back the same articles that were collected; no synthesis. |
| **3** | **Persist the training-fold cluster count.** Add a second argument so `train_cluster_count` holds train clusters and the evaluation keeps the test count. | High for *diagnosis* — this audit exists because the two were conflated. | **Low.** Additive; one more value through one call site. | A reporting correction, not a data change. |
| **4** | **Make the design matrix report its own sparsity and fail loudly.** `fit_ridge` returning `insufficient_data` after discarding 833 of 835 rows is correct but silent; the row count before and after `_clean_rows`, and the column responsible, belong in the persisted notes. | High. Converts a three-week-long invisible failure into an immediate one. | **Low.** Diagnostics only; no change to fitting or to the no-imputation policy. | Reports; changes nothing statistically. |
| **5** | **Reconsider union-of-columns for the feature set.** A feature present on 9 % of rows contributes almost nothing yet deletes 91 % of the data. Selecting a feature set by coverage *before* building the matrix — with the choice recorded in `feature_set_id` — would keep ~1,250 rows × 15 well-covered columns. | Medium-High. Would have let run #18 fit on ~1,250 rows. | **Medium.** Must be a declared, versioned feature-set decision, not a silent drop, or it becomes selection on convenience. | Drops *columns*, never rows; does not touch clustering or the sample floor. |
| **6** | **Widen instrument coverage.** 244 → 301 → ≤389 clusters; the registry has 389 instruments and 351 have cached candles. | Medium, and genuinely raises effective sample. | Low. | A new instrument is a genuinely new cluster — `sample_adequacy.py:41` lists this first among legitimate increases. |
| **7** | **Let labels mature.** d5 is resolved on 1,194 of 1,295 observations. | Low-Medium, automatic with the calendar. | None. | `sample_adequacy.py:42` lists label maturation as legitimate. |

Options 1 and 2 are the same problem approached from two ends and are **not alternatives** — 2
restores the inputs, 1 stops discarding the derivations. Doing 2 without 1 would still leave the
stale sweeps deleting events on any run where derivation happens to differ.

---

## 8. Risks and limitations

1. **Both local databases are stale** (51 and 28 days). Every Nov 1 figure comes from the
   committed dashboard, never from them. The historical tables are labelled by date throughout.
2. **`feature_coverage` shows only `source='phase8_feature_engine'`.** The design matrix also
   includes Phase-6 features (`entity.*`, `event.quality_confidence`, `liquidity.baseline_*`,
   `volatility.pre_volatility` — 9 names, 95-100 % covered on Sep 11). Their Nov 1 state is
   **unknown**. If any of them also collapsed, the complete-case ceiling is **below** 111, which
   would strengthen the conclusion, not weaken it.
3. **The exact Nov 1 complete-case count and training-cluster count are not measured.** Known
   only as `< 2` and `≤ 389`.
4. **Run logs are inaccessible** (403; no `gh`; no `actions:read` token). `train_models.py` prints
   the quality histogram, usable rows, numeric feature count, split sizes and both cluster counts
   — all of which would make §6 step 7 direct rather than inferential.
5. **The 1:1 observation↔event relation is measured on two snapshots, not proven invariant.**
   `observation_id_for(event_id, instrument_id)` implies a many-to-one relation is possible
   (one event, several instruments); it simply has not occurred in the data examined.
6. **Why `canonical_events` fell to 129 is attributed, not observed.** The deletion statements,
   the article-derivation path and the before/after counts are all verified; the actual run in
   which the sweep removed them was not observed, because that requires the logs or production.
7. **No causal claim is made about runs #11–#15.** This audit explains run #18's model, not those
   failures.
8. **No remedy here touches a gate.** The `MIN_EFFECTIVE_SAMPLE` floor, the leakage checks, the
   embargo, the holdout and the qualification rules are all left exactly as they are. Clustering
   by instrument is endorsed, not loosened.

---

## 9. Inspected files and commands

### Source read (all at `origin/main` = `1b6e216`)

```
scripts/train_models.py                  (1-438, in full)
scripts/compute_features.py              (67-329; the LEFT JOIN at 228-235)
scripts/build_research_observations.py   (240-283; cluster key at 251)
scripts/populate_events.py               (115; stale sweep 250-265)
scripts/fuse_events.py                   (139, 170-190)
scripts/archive_old_articles.py          (1-40 docstring; DELETE at 192)
scripts/reduce_database_size.py          (11-34, 88-121 — ruled out)
src/modeling/algorithms.py               (36-51 _clean_rows; 100-135 fit_ridge; 250-267 predict)
src/modeling/engine.py                   (228-300 train; 319-420 evaluate/train_and_evaluate)
src/adaptive/sample_adequacy.py          (1-120)
src/dashboard.py                         (227-241 _collect_research; 243-275 _collect_models)
src/data_access/research_schema.py
.github/workflows/daily.yml              (118-131)
.github/workflows/pipeline.yml           (176-197)
```

### Commands

```
git ls-tree <rev> <path> ; git cat-file blob <sha>        (rev:path is mangled by this shell)
git log --since=2026-10-06 --until=2026-11-02 origin/main -- scripts/compute_features.py \
        src/modeling/ src/research/ scripts/build_research_observations.py     -> EMPTY
git grep -n "event_cluster_id" / "insufficient_data" / "DELETE FROM" ...
grep -rn "archives/" --include=*.py scripts/ src/                             -> writer only
ls data/archives/ | wc -l  ;  du -sh data/archives/                           -> 35 files, 24 MB
```

### Databases

Three scratchpad scripts, each run as `python -I <script> <db>` with
`sqlite3.connect("file:…?mode=ro", uri=True)`:

- `cluster_audit.py` — provenance, cluster-size distribution, time coverage, duplication,
  feature-matrix shape, label resolution, and a faithful replay of `load_dataset` +
  `chronological_split` + `_clean_rows`.
- `feature_source_audit.py` — per-feature coverage split by `source`, matching the dashboard's own
  metric so Sep 11 and Nov 1 are directly comparable.
- an upstream-volume comparison across `articles`, `news_articles`, `article_entities`,
  `canonical_events`, `events`, `research_observations`.

Run against `data/marketlens.db` and `data/marketlens_corpus_v2.db` **only**. The production
database, the release asset and all three backups were never opened.

### Nov 1 production figures

`docs/index.html` at `1b6e216`, decoded with an explicit `utf-8`/`replace` decode; `D["research"]`,
`D["models"]`, `D["events"]`, `D["impact"]`, `D["health"]`, `D["flow"]`. Model-tuple column order
taken from `src/dashboard.py:254-261` rather than guessed.

---

## 10. Final status

**CAUSE IDENTIFIED**

The premise is corrected and the real cause is established.

**The 79 is a test-fold instrument count mislabeled as a training-fold count** —
`engine.py:259` writes the single `cluster_count` argument, given `test_clusters`, into
`trained_models.train_cluster_count`. Reproducing the 2026-08-29 record exactly (320 rows / 4 test
clusters, persisted as `train_sample_size=320, train_cluster_count=4`, while the true training
fold had **171** clusters) proves the mislabel rather than inferring it. The training cluster
count for run #18 was never stored.

**Clustering is not the problem.** A cluster is an instrument, assigned literally
(`event_cluster_id=instrument_id`), which is the statistically conservative and correct choice.
The distribution is healthy: 2.4-3.5 observations per cluster, median 2-3, the ten largest
clusters holding 11-13 % of rows, 19-42 % singletons. Nothing is accidental or excessively broad,
and there is no row-level duplication — exactly one observation per event.

**The model could not be fitted because the design matrix had fewer than two complete rows.**
`fit_ridge` sets `insufficient_data` on `len(clean_X) < 2` and nothing else; `_clean_rows` applies
listwise deletion across the **union** of all numeric feature columns.

**That emptiness is an upstream retention failure.** `daily.yml` archives articles older than 60
days out of the live `articles` table; `populate_events.py` and `fuse_events.py` then **delete**
the derived events they can no longer re-derive from it (846 → 153 events, 581 → 129 canonical
events); `research_observations` meanwhile only ever accumulates (→ 1,295). `compute_features.py`
LEFT JOINs the two, so ~90 % of observations resolve to NULL event metadata, and seven
article/event-derived features fall from ~100 % coverage to **8.6-10.3 %** while every
price-derived feature holds at ~96-97 %. Counts survived because zero is a number; means,
dispersions and confidences did not, because they are undefined over an empty set. The rarest
column, `sentiment.dispersion_7d` at **111 of 1,295**, caps complete-case rows at **8.6 %**, and
the oldest-70 % training fold contained fewer than two of them.

The articles themselves were never lost — 35 gzipped archive files, 24 MB, 2023-10 to 2026-09 sit
in `data/archives/`, exactly as `archive_old_articles.py` promised. **Nothing reads them back.**

---

## 11. Recommended next action

**Make the two stale sweeps stop deleting events whose source articles were merely archived — in
`scripts/populate_events.py:259-263` and `scripts/fuse_events.py:178-182` — so the
observation→event join is restored for the ~90 % of the corpus that currently cannot resolve it.**

This is the single highest-impact change and the narrowest. It touches two `DELETE` loops, not the
feature engine, not the design matrix, not clustering, and not one gate. The sweeps were written to
clear orphans left by the uuid4 report-id migration, and that purpose is preserved by narrowing
them: delete a row that this run *contradicts*, never one it simply *cannot see* because the source
article has aged out of the live table.

Pair it with reading `data/archives/` back during feature computation (recommendation 2) — the
evidence is already on disk, complete and compressed, and `archive_old_articles.py`'s own docstring
says Phase 7 needs precisely those rows.

Do this before adding instruments, widening windows or touching the model. Restoring the join
should return roughly 1,250 complete-case rows across ~15 well-covered features and let the ridge
fit on evidence that already exists. Only then is a fit result worth interpreting — and whether
the market is predictable remains a question this dataset has not yet been asked.
