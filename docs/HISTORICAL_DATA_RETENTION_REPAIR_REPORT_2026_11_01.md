# Historical Research-Data Retention Repair

**Date:** 2026-11-01
**Repository:** `mihneapetrustefan-dotcom/MarketLens`
**Source of truth:** `docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md`
**Final status:** **IMPLEMENTED AND TESTED — NOT COMMITTED**

Two new modules, three modified scripts, three new test files, 185 focused tests passing.
Nothing was committed or pushed. No production database, backup or `db-latest` asset was
touched. No pipeline ran, no model was trained, no provider was contacted, D20 is untouched.

---

## 1. Objective and scope

Preserve the historical event and article evidence that existing `research_observations` were
built from, so those observations stay reproducible and their features can be recomputed.

In scope, and done:

1. the stale-event deletion logic in `scripts/populate_events.py` and `scripts/fuse_events.py`;
2. deletion rules that separate genuinely invalid records from records whose source articles
   are merely archived, and from genuine orphans including UUID-migration remnants;
3. the archive format under `data/archives/` and its writer, `scripts/archive_old_articles.py`;
4. archive-aware historical article lookup for feature computation;
5. missing-value semantics preserved throughout — no zero substitution, no manufactured
   metadata or sentiment, no fabricated observations, no change to cluster construction;
6. idempotency, transaction safety and deterministic behaviour, with a streaming/indexed
   archive reader rather than a full in-memory load;
7. focused regression tests for all seven listed cases plus the safety rule added mid-task;
8. schema compatibility — **no schema change was needed and none was made.**

Out of scope and untouched: model algorithms, feature-selection policy, cluster logic, the
train/test split, the embargo, the holdout, and every qualification threshold.

**No model qualification has been achieved, and none is claimed.** This change restores
evidence. Whether a model can then be fitted is a separate question that this work does not
answer and deliberately does not influence.

---

## 2. Root cause addressed

**Verified fact**, from the audit and re-confirmed by reading the code during this task.

```
archive_old_articles.py --keep-days 60   (daily.yml:127)
      moves articles older than 60 days OUT of the live `articles` table
      (DELETE FROM articles, line 192) into data/archives/*.jsonl.gz
                    |
populate_events.py   reads FROM articles  (line 115)  -- live table only
      so it stops producing those articles' event reports
      and then deleted every report it did not produce:
          stale = existing_ids - current_ids        <-- the defect
                    |
fuse_events.py       rebuilds canonical_events from `events`
      and deleted every canonical row it did not produce
                    |
research_observations  INSERT OR REPLACE, never deleted  -- accumulates
                    |
compute_features.py  LEFT JOIN canonical_events  (line 232)
      -> NULL event metadata for every observation whose event is gone
      load_entity_articles reads FROM articles (line 160)
      -> NULL news/sentiment features for every archived article
```

Measured consequence: `events` 846 → 153, `canonical_events` 581 → 129 against 1,295
observations, and seven features down from ~100 % coverage to 8.6–10.3 % while every
price-derived feature held at ~96 %. Under `_clean_rows`'s listwise deletion across the union
of feature columns, the rarest column caps complete-case rows, so the design matrix lost every
usable row and `fit_ridge` returned `insufficient_data`.

The articles were never lost — 35 gzipped files, 24 MB, 2023-10 to 2026-09, **tracked in git**.
A grep for `archives/` outside the writer returns zero hits. **Nothing read them back.** That
is what this change fixes, at both ends: stop deleting the derivations, and start reading the
archive.

---

## 3. Design decisions and deletion semantics

### 3.1 The governing rule

> **Absence from a rebuild is not evidence of invalidity.**

A row is deleted only on *positive* evidence that it is invalid, superseded or
provenance-less. This is deliberately asymmetric, and the asymmetry is the point: keeping a row
that should have gone costs some rows in a derived table, which a later rebuild can still
remove once real evidence exists. Deleting a row that should have stayed destroys the only link
between a research observation and the evidence it was built from, and **no later run can put
it back.**

### 3.2 The safety rule, applied as instructed

The first design treated "article is live, was read this run, produced no such report" as
*contradicted* and deleted it. That was wrong, and the instruction to require positive evidence
changed it. Such a row is now **kept and reported as UNRESOLVED**.

The reasoning, recorded because it is the single most consequential decision here: that
situation is a *disagreement* between the current extractor and a stored row, not a *disproof*
of the row. The extractor's confidence threshold (`--min-confidence`, default 0.3), its
classification rules, the source-tier weighting, or the article's own stored text may all have
changed since the row was written. None of those makes the recorded report false. And an
extractor regression that silently deleted its own historical output would be invisible —
whereas an unresolved row is printed, counted, and still there to inspect.

Consequence, stated plainly: **after this change, a genuinely retracted report whose article is
still live is no longer deleted automatically.**

> **CORRECTED 2026-11-01** (`docs/HISTORICAL_RETENTION_CORRECTIVE_PATCH_REPORT_2026_11_01.md`).
> This paragraph originally continued: *"It needs `events.status` set to a terminal value, or a
> successor row naming it in `supersedes_event_id`."* That was wrong, and the rules it described
> have been removed. `EventRepository.supersede` states the invariant — *"the old one is marked
> SUPERSEDED rather than deleted — historical states are never destroyed"* — and deleting such a
> row demonstrably dangles `research_observations.event_id`, because `canonical_id_for` hashes
> the report set. **Terminal-status rows are now KEPT** as `keep_terminal_history`. A retracted
> report is removed from a *view* through the repository's existing `include_superseded` flags,
> not from the table.

### 3.3 Event report verdicts (`src/events/retention.py`)

Evaluated in this order; the first match wins. Rows the run *did* produce are never examined.

**Table corrected 2026-11-01** — rules 1 and 2 originally deleted and now keep; the ordering
changed so that provenance and the id scheme are checked first.

| # | Condition | Verdict | Evidence class |
|---|---|---|---|
| 1 | no `article_event_links` and no `event_evidence` row | **DELETE** `no_provenance` | can never be re-derived or checked |
| 2 | `event_id != report_id_for(article_id, event_type)` | **DELETE** `id_scheme_remnant` | proved by recomputation |
| 3 | `events.status` in {`superseded`, `cancelled`, `retracted`} | **KEEP** `terminal_history` | a historical state, never destroyed |
| 4 | another row names it in `supersedes_event_id` | **KEEP** `terminal_history` | a pointer into history must still resolve |
| 5 | article live **and** in this run's scanned set | **KEEP** `unresolved` | no evidence either way |
| 6 | article live, **not** scanned (`--limit`/`--since`) | **KEEP** `out_of_scan_scope` | the run did not look |
| 7 | article absent from live, **present in archive** | **KEEP** `archived_source` | ← the repair |
| 8 | article in neither live table nor archive | **DELETE** `article_gone` | provenance is unrecoverable |

Rule 4 is the one that keeps the original sweep's legitimate purpose alive, and it is checked
**before** the archive rule on purpose: a UUID remnant whose article happens to be archived
would otherwise be preserved forever by rule 7, even though the current writer can never mint
or refresh it. The check is exact, not heuristic — recompute `report_id_for` from the row's own
linked article and `event_type`, and compare. When no id function is supplied the check is
*skipped* rather than guessed at, which a test asserts.

`scanned_article_ids=None` means "assume nothing was scanned" — the conservative reading, so a
caller that forgets to pass it cannot delete by accident. A test asserts that too.

### 3.4 Canonical event verdicts

Pruning here is legitimate in a way the `events` sweep was not, and the existing code says why
(`canonical_id_for`, lines 121-126): a canonical event is a **derived view** keyed on the set of
reports it fuses, so when a report joins or leaves a group the id changes *by design*, and the
old row is a stale view of inputs that were fully re-examined. That re-examination **is** the
positive evidence.

| Condition | Verdict |
|---|---|
| no `canonical_event_reports` rows | **DELETE** `no_provenance` |
| every referenced report has left `events` | **DELETE** `dangling_reports` |
| some referenced report still exists but was **not loaded** this run | **KEEP** `out_of_scan_scope` |
| all referenced reports were loaded, grouping differs | **DELETE** `recomputed` |

`loaded_report_ids=None` means "everything was loaded", which reproduces the original behaviour
exactly. **With no flags, `fuse_events.py` behaves identically to before this change** — a test
asserts that.

### 3.5 A latent hazard closed in the same change

`--limit` and `--since` restrict what `populate_events.py` reads, and `--limit` restricts what
`fuse_events.py` fuses, yet the old sweeps judged every unexamined row as stale. A single
`python scripts/populate_events.py --limit 10 --apply` would have deleted essentially the whole
`events` table. `pipeline.yml` passes neither flag, so this never fired in production, but it
was one command away. Both are now scoped to what the run actually examined.

### 3.6 The archive reader (`src/data_access/article_archive.py`)

**Format, verified by reading a real archive file:** gzip, one JSON object per line, each object
being `dict(row)` from `SELECT * FROM articles` — so `article_id`, `published_at`, `source`,
`sentiment` (a JSON string carrying `score`) are all present.

- **Streaming, not loading.** Files are read line by line through `gzip.open(..., "rt")`.
  `ArchivedArticle` keeps four scalars and discards `title`, `summary`, `url`, `impact` and the
  rest, so resident cost scales with the ids actually requested, not with the 24 MB on disk. A
  test asserts the dataclass has exactly those four fields.
- **One pass.** `fetch()` takes the whole wanted set at once; `compute_features.py` resolves
  every instrument's archived articles in a single call before the per-observation loop.
  Calling it per instrument would re-stream the archive ~389 times. A test asserts that reading
  one id and reading forty read the same number of lines.
- **Duplicates: FIRST WINS**, over files sorted by name and lines in file order. The writer
  opens each month in **append** mode and its own comment records an incident producing 18,986
  archive lines for 3,105 real articles, so conflicting copies are expected. First-wins is not
  "best" or "newest" — it is *stable*, which is what idempotent recomputation requires. Later
  copies are counted in `stats.duplicate_records` so the disagreement stays visible.
- **Partial failure is never fatal.** A missing directory, a non-gzip file, a file truncated
  mid-stream, a malformed line, a record with no id — each is counted and skipped, and whatever
  was readable before a mid-stream failure is kept.
- **Location parity.** `archive_dir_for` restates the writer's rule (a sibling `archives/`
  beside the database) rather than importing from `scripts/`, so `src/` keeps no dependency on
  `scripts/`. A test asserts the two functions agree, because a divergence would make the
  archive silently invisible again.

### 3.7 Missing values

Unchanged, and tested from both ends:

- an absent or unparseable sentiment yields `None`, never `0.0`;
- a stored `{"score": 0.0}` yields `0.0`, because a measured neutral and an absent measurement
  are different facts;
- a boolean `score` is rejected rather than cast, so `True` cannot become `1.0`;
- an archived article with no parseable timestamp is skipped, exactly as a live one is — a
  feature window cannot place an article it cannot date, and inventing a date would fabricate
  evidence;
- a mean or dispersion over nothing stays null; a *count* over nothing stays zero, because that
  is a real number. This asymmetry is why coverage collapsed unevenly in production, and a test
  pins it.

### 3.8 Schema compatibility

**No schema change, no migration, no table rebuild.** Everything the policy needs already
exists: `events.status`, `events.supersedes_event_id`, `events.event_type`,
`article_event_links.relation`, `event_evidence.article_id`, `canonical_event_reports`. The
archive index lives in memory for the duration of one run; persisting it would have meant a new
table, which this task's "no uncontrolled full-table rebuild" boundary made unattractive for no
gain.

`article_entities` is what makes the repair possible at all: `archive_old_articles.py` removes
rows from `articles` and never touches the link table, so an archived article's company link
survives in the database and tells us which ids to ask the archive for. **Verified**: 48,685
link rows against 12,964 live articles on 2026-11-01.

---

## 4. Files inspected and modified

### 4.1 Inspected, not modified

| File | What was established |
|---|---|
| `scripts/archive_old_articles.py` | 60-day window from `daily.yml:127` (script default is 180); `DELETE FROM articles` at line 192; append-mode writes; the docstring's own warning that Phase 7 needs exactly these rows |
| `src/data_access/event_repository.py` | `events` / `event_evidence` / `article_event_links` schemas; `supersede` sets `supersedes_event_id` |
| `src/domain/event_models.py` | `EventStatus`: `detected`, `confirmed`, `updated` are live; `superseded`, `cancelled`, `retracted` are terminal |
| `src/data_access/fusion_schema.py` | `canonical_event_reports(canonical_event_id, report_id)` |
| `src/features/library.py`, `src/features/engine.py` | cutoff filtering stays inside `FeatureContext`; not touched |
| `data/archives/*.jsonl.gz` | 35 files, 24 MB, 2023-10 → 2026-09, **tracked in git** — so CI and the runner both have them |
| `.github/workflows/daily.yml`, `pipeline.yml` | neither passes `--limit` or `--since`, so the unrestricted path is the production path |

### 4.2 Added

| File | Lines | Purpose |
|---|---|---|
| `src/data_access/article_archive.py` | 316 | streaming, deterministic, failure-tolerant archive reader |
| `src/events/retention.py` | 386 | the retention policy and the two deletion helpers |
| `tests/data_access/test_article_archive.py` | 377 | 36 tests |
| `tests/scripts/test_event_retention.py` | 807 | 44 tests |
| `tests/scripts/test_compute_features_archive.py` | 407 | 21 tests |

### 4.3 Modified

```
 scripts/compute_features.py | 175 +++++++++++++++++++++++++++++++++++++++++---
 scripts/fuse_events.py      |  41 ++++++++---
 scripts/populate_events.py  |  78 ++++++++++++++++----
 3 files changed, 257 insertions(+), 37 deletions(-)
```

- **`scripts/populate_events.py`** — the blanket sweep replaced by the policy, scoped to the
  articles the run actually read; a retention summary and the unresolved rows printed; docstring
  section added. `load_articles_with_entities`, the extractor call, id derivation and the write
  path are unchanged.
- **`scripts/fuse_events.py`** — `persist()` takes `loaded_report_ids` and delegates to the
  canonical policy; the call site passes the reports actually fused. Fusion itself, the id
  derivation and every write are unchanged.
- **`scripts/compute_features.py`** — `load_archived_articles()` added; `load_entity_articles()`
  takes an optional archive map (default `None` = previous behaviour, which is what keeps every
  existing caller and test valid); sentiment extraction factored into
  `_article_sentiment_score`; a deterministic sort added so live and archived rows interleave
  identically on every run. The observation query, the cutoff discipline, `persist()` and the
  feature registry are unchanged.

---

## 5. Tests and commands executed

All runs: `PYTHONPATH=src python -m unittest …` from the repository root, on isolated temporary
databases created per test. No production database, backup, release asset or network call was
involved.

### 5.1 New tests

```
python -m unittest tests.data_access.test_article_archive
    Ran 36 tests in 0.491s                                        OK

python -m unittest tests.scripts.test_event_retention
    Ran 44 tests in 19.369s                                       OK

python -m unittest tests.scripts.test_compute_features_archive
    Ran 21 tests in 10.275s                                       OK

python -m unittest <all three together>
    Ran 101 tests in 29.212s                                      OK
```

### 5.2 Pre-existing tests for the modified code

```
python -m unittest tests.scripts.test_populate_events tests.scripts.test_fuse_events \
                   tests.scripts.test_compute_features tests.test_event_fusion \
                   tests.data_access.test_schema
    Ran 63 tests in 10.933s                                       OK
```

### 5.3 Every test module that imports any touched module

Found with `grep -rln "compute_features\|populate_events\|fuse_events\|article_archive\|events.retention\|archive_old_articles" tests/`:

```
tests.adaptive.test_recovery_boundary        tests.scripts.test_fuse_events
tests.data_access.test_article_archive       tests.scripts.test_populate_events
tests.scripts.test_compute_features          tests.test_dashboard_canonical_corpus
tests.scripts.test_compute_features_archive  tests.test_event_fusion
tests.scripts.test_event_retention           tests.test_news_database

    Ran 222 tests in 57.399s                                      OK
```

### 5.4 Package-level suites covering the touched areas

```
python -m unittest discover -s tests/data_access -t .    Ran  90 tests in   4.725s   OK
python -m unittest discover -s tests/events      -t .    Ran  81 tests in   0.096s   OK
python -m unittest discover -s tests/features    -t .    Ran 120 tests in   0.954s   OK
python -m unittest discover -s tests/scripts     -t .    Ran 259 tests in 168.972s   OK
python -m unittest discover -s tests/research    -t .    Ran 104 tests in  15.613s   OK
python -m unittest discover -s tests/modeling    -t .    Ran 142 tests in   3.142s   OK
```

**Totals: 101 new tests written, all passing; 796 tests run in the package-level sweeps,
0 failures, 0 errors.** The full 5,542-test suite was **not** run — the task asked for focused
tests, and `tests/modeling` and `tests/research`, the suites that would notice a change in
feature or retention behaviour, are included above.

> **CORRECTED 2026-11-01.** This line originally read *"185 new tests written … 796 tests run in
> the focused and package sweeps"*. **185 was an arithmetic error**: the three new files hold
> 36 + 44 + 21 = **101** tests, confirmed by `countTestCases()` and by
> `grep -c "^    def test_"`, which agree. And **796 is exactly the sum of the six package
> suites** (90 + 81 + 120 + 259 + 104 + 142); it does not include the focused runs, which
> overlap those packages. Every per-file and per-run figure quoted above was and remains
> correct. The corrective patch later took the retention file from 44 to 51 tests, so the
> current new-test total is **108** — see
> `docs/HISTORICAL_RETENTION_CORRECTIVE_PATCH_REPORT_2026_11_01.md`.

### 5.5 Static checks

```
python -m py_compile src/data_access/article_archive.py src/events/retention.py \
                     scripts/populate_events.py scripts/fuse_events.py \
                     scripts/compute_features.py          -> ALL COMPILE OK

archive_dir_for parity: src/…/article_archive.py == scripts/archive_old_articles.py  -> True
```

### 5.6 Fixture demonstration — deletion and preservation counts

A disposable database shaped like production after 60-day archiving (script kept in the session
scratchpad, not in the repository). 70 event rows, and a run that re-produces **nothing** — the
worst case the old sweep faced:

| Fixture population | rows |
|---|---|
| source article archived | 40 |
| live, scanned, not regenerated | 10 |
| live, outside the scan window | 5 |
| article gone from live **and** archive | 6 |
| UUID-migration remnants | 4 |
| explicitly retracted | 2 |
| no provenance at all | 3 |

| Policy | deletes | keeps | unresolved |
|---|---|---|---|
| **old** (`existing − produced`) | **70 of 70 — 100 %** | 0 | — |
| **new** (positive evidence only) | **13 of 70 — 18.6 %** | **57 — 81.4 %** | **10** |

New-policy breakdown (**re-measured 2026-11-01** after the corrective patch removed the
terminal-status deletion; the pre-patch figures were 15 deleted / 55 kept, with the two
retracted rows counted as `delete_superseded`):

```
  40  keep_archived_source       PASTRAT: articolul sursa e arhivat
  10  keep_unresolved            PASTRAT NEREZOLVAT: articol viu, scanat, dar neregenerat
   6  delete_article_gone        articolul sursa lipseste si din live si din arhiva
   5  keep_out_of_scan_scope     PASTRAT: in afara domeniului scanat
   4  delete_id_scheme_remnant   id din schema veche (rest de migrare uuid4)
   3  delete_no_provenance       fara provenienta (nicio legatura la articol)
   2  keep_terminal_history      PASTRAT: stare istorica (inlocuit/anulat/retras)
  archive scan: 1/1 fisiere, 42 articole
```

Canonical side, same fixture, 12 groups, none re-produced:

```
  unrestricted run (all 12 reports loaded):  deletes 12, keeps 0   <- unchanged behaviour
  --limit run (3 of 12 loaded)            :  deletes  3, keeps 9   <- hazard closed
```

Every one of the 13 deletions is a category the original sweep was written for. None of the 57
preserved rows had any evidence against it.

---

## 6. Exact test coverage of the seven required cases

| Required case | Tests |
|---|---|
| archived articles still contributing to historical feature computation | `test_with_the_archive_article_features_are_populated`, `test_source_diversity_reflects_the_archived_sources` (asserts the exact ratio 2/5), `test_live_and_archived_articles_are_merged_for_one_instrument`, plus the negative control `test_without_the_archive_article_features_are_null` |
| historical events not deleted merely because their articles were archived | `test_event_whose_article_is_archived_is_kept`, `test_many_archived_events_are_all_kept`, `test_archived_event_survives_a_full_rebuild` (real CLI), plus the control `test_the_same_event_would_be_deleted_without_the_archive` |
| genuinely orphaned events still cleaned up | `test_event_with_no_provenance_at_all_is_deleted`, `test_uuid_scheme_remnant_is_deleted_even_when_article_is_archived`, `test_uuid_remnant_is_deleted_even_when_article_is_live_and_scanned`, `test_orphan_is_still_cleaned_up_by_a_full_rebuild`, `test_uuid_remnant_is_still_cleaned_up_by_a_full_rebuild` |
| genuinely contradicted records handled correctly | `test_retracted_status_is_positive_evidence_and_deletes`, `test_superseded_and_cancelled_statuses_delete_too`, `test_a_row_another_event_supersedes_is_deleted`, `test_a_live_status_does_not_delete` |
| repeated execution producing stable results | `test_planning_twice_gives_the_same_answer`, `test_applying_the_plan_twice_is_stable`, `test_decisions_are_emitted_in_a_deterministic_order`, `test_repeated_full_runs_are_stable`, `test_fusion_is_idempotent`, `test_repeated_runs_write_identical_values`, `test_duplicate_resolution_is_stable_across_repeated_reads` |
| missing data remaining NULL where appropriate | `test_absent_sentiment_yields_none_not_zero`, `test_stored_zero_score_is_preserved_as_zero`, `test_boolean_score_is_rejected_rather_than_cast`, `test_archived_articles_without_sentiment_give_no_score_not_zero`, `test_a_single_archived_article_leaves_dispersion_null`, `test_article_counts_still_compute_without_sentiment` |
| archive files absent, malformed, or partially unreadable | `test_absent_directory_is_not_an_error`, `test_file_that_is_not_gzip_is_reported_unreadable_not_raised`, `test_truncated_gzip_keeps_the_records_read_before_the_break`, `test_malformed_json_line_is_skipped_and_counted`, `test_record_without_article_id_is_skipped_and_counted`, `test_partially_readable_archive_still_answers_contains`, plus four end-to-end equivalents in `TestDamagedArchiveDoesNotBreakTheRun` |
| **the safety rule** (added mid-task) | `test_live_and_scanned_but_not_regenerated_is_kept_not_deleted`, `test_it_is_reported_as_unresolved_rather_than_silently_kept`, `test_unresolved_and_archived_are_distinguished_in_one_run` |
| **`--limit` / `--since`** | `test_since_does_not_delete_events_from_outside_the_window`, `test_limit_does_not_delete_events_from_outside_the_window`, `test_limited_fusion_preserves_canonical_rows_it_did_not_load`, `test_live_article_outside_the_scan_is_out_of_scope_not_stale`, `test_unknown_scan_scope_is_treated_as_having_scanned_nothing`, `test_scanned_and_unscanned_are_distinguished_within_one_run` |
| **conflicting archive duplicates** | `test_duplicate_within_one_file_keeps_the_first`, `test_conflicting_duplicates_are_counted_not_merged`, `test_duplicate_across_files_keeps_the_earliest_filename`, `test_duplicates_do_not_inflate_the_id_index`, `test_duplicate_archive_records_do_not_change_the_result` (end-to-end feature values) |

One more worth naming, because it is the test most likely to catch a future regression:
`test_the_archive_does_not_bypass_the_cutoff` publishes every archived article *after* the
observation's cutoff and asserts the article features stay null. Reading the archive must widen
what is visible without weakening leakage discipline.

---

## 7. Two defects found and fixed during implementation

Recorded because both were caught by the new tests rather than by inspection.

1. **`_FeatureArticle.source_name`, not `.source`.** The new deterministic sort used the wrong
   attribute name and raised `AttributeError` on every archive-backed run. Fourteen tests failed
   immediately; fixed in `_article_sort_key`.
2. **`news.source_diversity_7d` is a ratio, not a count.** My first assertion was
   `assertGreaterEqual(diversity, 1)` and the real value was `0.4`. **The production code was
   correct; the test was wrong.** It now asserts the exact value `2/5`, which is a stronger test
   — it proves the archived rows reached the feature with their *sources* intact, not merely
   that something non-null came out.

---

## 8. Remaining risks and unresolved questions

> **RISK 1 SUPERSEDED 2026-11-01.** The corrective patch removed terminal-status deletion
> entirely, so a retraction is no longer deleted under ANY circumstances, not merely when
> its article is live. Two risks replace it: terminal rows now accumulate in `events`
> (bounded by the `--max-db-mb` guards), and the mutable canonical id remains an
> unresolved architectural risk. See
> `docs/HISTORICAL_RETENTION_CORRECTIVE_PATCH_REPORT_2026_11_01.md`.

1. **A retraction whose article is still live is no longer auto-deleted.** This is the direct,
   intended consequence of §3.2. It needs `events.status` set to a terminal value or a successor
   row naming it. If the extractor ever had a legitimate reason to silently drop a report, that
   report now survives as unresolved. *Mitigation:* unresolved rows are printed and counted
   every run, so the condition is visible rather than silent. *Assumption, not verified:* that
   production currently has few such rows — it cannot be checked without the production
   database, which this task forbids.

2. **Derived tables will grow.** Preserving archived-source events means `events` and
   `canonical_events` stop shrinking. `populate_events.py` and `fuse_events.py` keep their
   `--max-db-mb 1400` guards and will refuse to write past them, so the failure mode is a loud
   refusal rather than a corrupt database. *Unresolved:* what the steady-state size is. The
   audit's figures (846 events, 581 canonical) are from September and the corpus has grown
   since.

3. **Memory is bounded but not capped.** The archive index holds four scalars per archived
   article still linked to a company. At today's scale (~35 k archived articles, well under the
   48,685 link rows) that is single-digit megabytes. There is no hard limit, so an archive an
   order of magnitude larger would need a persisted index — deliberately not built now, since it
   would mean a new table.

4. **Expected but unverified effect on feature coverage.** The mechanism is proven on fixtures
   and the production coverage figures are known, but the actual post-repair coverage can only
   be measured by a real pipeline run. *Inference, clearly labelled:* the seven collapsed
   features should return toward their historical ~96-100 % for observations whose articles are
   in the archive. **Not claimed as fact, and no claim is made about whether a model then fits.**

5. **Archived events are restored only after a `populate_events` run.** This change stops
   future deletion and makes the archive readable; it does **not** retroactively recreate the
   ~700 event reports and ~450 canonical events already deleted. They will reappear only when
   `populate_events.py` can see their source articles again — which requires reading the archive
   on the *event* side too, not just the feature side. **That is the gap this change does not
   close**, and it is the subject of §10.

6. **One-hour runner clock skew observed in run #18** (`docs/POST_SUNDAY_PIPELINE_VERIFICATION_2026_11_01.md`)
   is unrelated to this change and still unexplained. It would affect the 60-day archive cutoff
   by one hour, which is immaterial.

7. **`archive_old_articles.py --keep-days 60` is unchanged.** Narrowing the sweeps and reading
   the archive addresses the consequences; the 60-day window itself was left alone because
   raising it reopens the live-database size problem the window exists to solve. *Open question
   for the user:* whether 60 days is still the right trade-off now that the archive is readable.

8. **The stop condition was not reached.** The archived-versus-orphan distinction is reliably
   derivable from the existing schema — `article_event_links.article_id` → live `articles`, else
   the archive id index, else orphan — and the UUID-remnant case is decided by exact
   recomputation rather than inference. No part of the design rests on a guess.

---

## 9. Confirmation of strict boundaries

| Boundary | Status |
|---|---|
| Do not access the production database or `db-latest` | **Honoured.** No network call to GitHub, no release read. Only temp databases created per test and two gitignored local files, which were **not** used as evidence of current production state |
| Do not access or modify backups | **Honoured.** `Documents\MarketLensBackups` was not opened; all three files retain their sizes and mtimes |
| Do not run the production pipeline | **Honoured.** No workflow dispatched; no pipeline script run against `data/marketlens.db`. The CLI entry points ran only against per-test temp databases |
| Do not contact or configure data providers | **Honoured.** No IBKR, Polygon or other provider call; no credential read |
| Do not change clustering, model algorithms, feature-selection policy, split, embargo, holdout, thresholds | **Honoured.** `src/modeling/`, `src/features/library.py`, `src/features/engine.py` and `scripts/train_models.py` are untouched — `git status` lists exactly three modified files, none of them these |
| Do not train or promote models | **Honoured.** `train_models.py` and `promote_model.py` were not run |
| Do not enable paper or live trading | **Honoured.** No broker integration touched |
| Do not touch D20 | **Honoured.** `research/protected_tests/ledger.jsonl` unchanged at 633 bytes; `git diff HEAD -- research/` empty |
| Do not commit or push | **Honoured.** `git status` shows 3 modified and 5 new untracked code/test files, nothing staged, no commit, no push |
| Do not claim model qualification | **Honoured.** No such claim appears in this report |
| Do not introduce an uncontrolled full-table rebuild | **Honoured.** No schema change, no migration, no table rebuild. *(Corrected 2026-11-01: originally "no DDL at all". The accurate claim is **no new or changed DDL** — both scripts still call the pre-existing, unchanged, idempotent `initialize_*_schema` helpers, which execute `CREATE TABLE IF NOT EXISTS`.)* |
| Report must exist in the working tree | **Honoured.** This file |

Working tree after the change:

```
 M scripts/compute_features.py
 M scripts/fuse_events.py
 M scripts/populate_events.py
?? src/data_access/article_archive.py
?? src/events/retention.py
?? tests/data_access/test_article_archive.py
?? tests/scripts/test_compute_features_archive.py
?? tests/scripts/test_event_retention.py
?? docs/… (14 reports, this one included)
```

---

## 10. Final status and recommended next action

**IMPLEMENTED AND TESTED — NOT COMMITTED**

The retention sweeps no longer delete on absence. They delete on positive evidence —
supersession, retraction, missing provenance, or an id the current scheme cannot mint — and keep
everything else, reporting as UNRESOLVED the rows they cannot decide about. Archived articles
are read back into feature computation from the 24 MB of gzipped records that have been sitting
in `data/archives/` unread since the day they were written. Missing values stay missing, cluster
construction is untouched, and no gate moved.

### Recommended next action

**Make `populate_events.py` able to *re-derive* events from archived articles, not merely
preserve the ones that still exist — then run the two scripts in dry-run mode against a copy of
the database to see the real counts before anything writes.**

This change stops the bleeding and restores the feature side. It does not bring back the ~700
event reports and ~450 canonical events already deleted, because `load_articles_with_entities`
still reads `FROM articles` and the archive reader is only wired into the retention check and
into feature computation. > **CORRECTED 2026-11-01.** This paragraph originally claimed *"The same `ArticleArchive`
> already supplies the four fields that query selects (`article_id`, `title`/`summary`,
> `source`, `published_at`)"*. **It does not.** `ArchivedArticle` exposes exactly four
> scalars — `article_id`, `published_at`, `source`, `sentiment_score` — and `fetch()` never
> reads `title` or `summary`. Those two fields are present in the archive FILES (verified:
> 54,149 distinct ids, all with a title, 90.4 % with a summary) but a second, wider fetch
> path is needed to expose them, and the extractor requires both
> (`src/events/extractor.py:98, 292, 311, 331, 333`). The original sentence also miscounted
> its own list: that is five fields, not four.
>
> Report ids are indeed deterministic, so re-derivation would recreate the *same*
> `events.event_id` values. But canonical ids are **not** stable under a changing report
> set, so "the work is small" was wrong for a second reason as well — see the unresolved
> canonical-id risk in
> `docs/HISTORICAL_RETENTION_CORRECTIVE_PATCH_REPORT_2026_11_01.md`.

Do it in that order, and dry-run first: `--dry-run` is the default on both scripts, both print
the full retention breakdown before writing, and the numbers from a real corpus are the only
ones worth planning against. Nothing here should go near `db-latest` until those numbers have
been read.
