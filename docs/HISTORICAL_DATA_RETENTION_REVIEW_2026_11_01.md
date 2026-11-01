# Review — Historical Research-Data Retention Repair

**Date:** 2026-11-01
**Repository:** `mihneapetrustefan-dotcom/MarketLens`
**Under review:** `docs/HISTORICAL_DATA_RETENTION_REPAIR_REPORT_2026_11_01.md` and the
working-tree change it describes.
**Final status:** **TWO DEFECTS CONFIRMED, ONE REPORTING ERROR CONFIRMED — CHANGE NOT SAFE TO
COMMIT AS IS**

Strictly read-only. No code was changed. Nothing was committed, pushed, staged or reverted. No
production database, backup or `db-latest` asset was opened. No pipeline ran, no provider was
contacted, D20 is untouched. The two demonstrations below ran against temporary databases
created and deleted inside the session scratchpad.

| # | Question | Answer |
|---|---|---|
| 1 | Archive reader contradiction | **Report is wrong.** `ArchivedArticle` exposes four scalars; `title` and `summary` are in the archive *files* but the reader never reads them. Re-derivation needs real work, listed in §1.4 |
| 2 | Terminal event deletion | **Deletion is unsafe and the rule should be removed.** It contradicts an explicit documented invariant and demonstrably dangles a research observation's event reference |
| 3 | Test count | **Report is wrong.** 101 new tests, not 185. Every other figure checks out |
| — | Diff and working tree | **Clean.** 3 modified, 5 added, 0 staged, no unrelated file touched |

---

## 1. Archive reader contradiction

### 1.1 The contradiction is real

The implementation report says both of these, 353 lines apart:

> line 172 — "`ArchivedArticle` keeps four scalars and discards `title`, `summary`, `url`,
> `impact` and the rest"

> line 525 — "The same `ArticleArchive` already supplies the four fields that query selects
> (`article_id`, `title`/`summary`, `source`, `published_at`)"

**The first is correct. The second is false.** It also miscounts its own list: `article_id`,
`title`, `summary`, `source`, `published_at` is five fields, not four — the sentence conflated
the reader's four-scalar shape with the live query's five columns.

### 1.2 What the code actually exposes — verified

`src/data_access/article_archive.py`, the dataclass:

```python
class ArchivedArticle:
    article_id: str
    published_at: Optional[str]
    source: Optional[str]
    sentiment_score: Optional[float]
```

and `fetch()` reads exactly three keys off each record — `published_at`, `source`, `sentiment`
— plus the `article_id` it matched on. `record["title"]` and `record["summary"]` are never
touched. A test pins this deliberately
(`test_only_scalars_are_retained_not_the_whole_record` asserts
`set(found.__dataclass_fields__) == {"article_id", "published_at", "source", "sentiment_score"}`).

### 1.3 The data is in the files; only the reader drops it

Read from the three most recent real archive files (read-only):

```
articles_2026-07.jsonl.gz  keys: article_id, category, collected_at, companies_mentioned,
                                 duplicate_group_id, duplicate_group_size, impact,
                                 published_at, sectors, sentiment, source, stored_at,
                                 summary, tickers_mentioned, title, url
   title present: True  (90 chars)   summary present: True  (255 chars)
articles_2026-08.jsonl.gz  title 68 chars, summary 66 chars
articles_2026-09.jsonl.gz  title 99 chars, summary 159 chars
```

Full archive census, all 35 files:

| Measure | Value |
|---|---|
| lines read | 73,821 |
| **distinct `article_id`** | **54,149** |
| duplicate lines | **19,672 (26.6 %)** |
| unreadable files | **0** |
| non-empty `title` | 54,149 (100 %) |
| non-empty `summary` | 48,952 (90.4 %) |
| `title` length mean / median / max | 88 / 85 / 376 |
| `summary` length mean / median / max | 99 / 83 / 1,540 |
| **`title` + `summary` if retained for every distinct id** | **9.7 MB of strings** (realistically 15-20 MB with object overhead) |

Two things worth recording beyond the question asked. The archive is **clean** — zero
unreadable files across all 35. And the duplicate rate is **26.6 %**, which is not a theoretical
concern: the writer's append-mode behaviour really has produced 19,672 redundant lines, so the
reader's first-wins determinism is load-bearing, not decorative.

### 1.4 What re-deriving events from archived articles genuinely requires

`scripts/populate_events.py:113-143` builds each article dict from
`SELECT a.article_id, a.title, a.summary, a.source, a.published_at FROM articles a`, and
`src/events/extractor.py` consumes:

| Extractor use | Field | Available from the archive today? |
|---|---|---|
| `extractor.py:98` relevance filter — `title` + `summary` lowercased | `title`, `summary` | **in the files, NOT in `ArchivedArticle`** |
| `extractor.py:292` classification — same concatenation | `title`, `summary` | **same gap** |
| `extractor.py:303` `publication_time` | `published_at` | yes |
| `extractor.py:307` evidence `article_id` | `article_id` | yes |
| `extractor.py:309` evidence `source_name` | `source` | yes |
| `extractor.py:311` evidence excerpt — `title[:300]` | `title` | **same gap** |
| `extractor.py:331/333` event title and description | `title` | **same gap** |
| `extractor.py:338` `ingestion_time` | `ingested_at` | not needed — the live path already passes `None` |
| `populate_events.py:201` source tier | `source` | yes |
| `populate_events.py:127-131` participants | `article_entities` | yes — never pruned, 48,685 rows |

So the gap is exactly **`title` and `summary`**, and it is a *reader* gap, not a data gap. The
work required:

1. **A second, wider fetch path** — not a widening of `ArchivedArticle`. The four-scalar shape
   exists to bound the feature path, which resolves ~54 k ids in one pass; adding ~10 MB of
   strings to that path would cost 2-3× its current footprint for data no feature uses. A
   separate `fetch_full()` returning title and summary, used only by `populate_events.py`, keeps
   the budget where it belongs.
2. **Union the live and archived article sets in `load_articles_with_entities`.** Two
   sub-problems the current SQL hides: `--since` is a SQL predicate on `a.published_at` and
   would need a Python-side equivalent for archived rows; and `ORDER BY a.published_at DESC` +
   `LIMIT` would have to span both sources or `--limit` stops meaning "the newest N".
3. **Nothing on the retention side.** Once archived articles are scanned, they enter
   `scanned_article_ids`; a re-derived event lands in `produced_ids`, and one that still
   produces nothing moves from `keep_archived_source` to `keep_unresolved`. Both keep. No new
   deletion risk. Verified by reading the policy's branch order.
4. **A hard prerequisite the implementation report missed entirely** — §2.5 below. Re-derivation
   *adds* reports to existing canonical groups, which churns their ids, which dangles every
   `research_observations.event_id` and `event_studies.event_id` built on them. Re-deriving
   before fixing that would manufacture the very breakage the repair exists to prevent, on a
   larger scale.

The report's claim that "the work is small and the idempotency guarantee already holds" is
half right. Report ids are deterministic, so re-derivation would indeed recreate the *same*
`events.event_id` values. But canonical ids are **not** stable under a changing report set, and
that is where the work actually is.

---

## 2. Terminal event deletion

**Recommendation: PRESERVE. The `DELETE_SUPERSEDED` verdict should be removed from the policy.**

### 2.1 The domain model says so explicitly

`src/data_access/event_repository.py:221-234`:

```python
def supersede(self, old_event_id: str, new_event: StructuredEvent) -> None:
    """
    Record an evolved event (spec §14): the new version points back
    at the old one, and the old one is marked SUPERSEDED rather
    than deleted — historical states are never destroyed.
    """
```

That is not an incidental comment; it is the stated contract of the only method that sets the
status. And the repository is built around *filtering* rather than removal:

- `list(...)` takes `include_superseded` and adds `e.status != 'superseded'` when false
  (lines 350-352);
- `count(include_superseded=False)` does the same (lines 408-412).

So the existing semantics of a terminal status are **"exclude from current views"**, and the
mechanism for doing that already exists. A retention policy that deletes those rows does not
implement the domain model — it contradicts it. The implementation report's §3.3 presented
terminal status as "the data says so", and that reading is wrong: the data says *this is no
longer current*, not *this may be destroyed*.

### 2.2 Who sets a terminal status today

Searched across `src/` and `scripts/`: **nothing**. The only `.supersede(` call site is
`src/signals/engine.py:170`, which is the *signal* repository, a different object.
`EventRepository.supersede` and `update_status` are exercised only by
`tests/events/test_event_repository.py`.

Consequence for risk calibration: the defect is **latent, not currently firing**. No production
row carries `superseded`, `cancelled` or `retracted` today, so the rule would delete nothing on
the next run. It is a trap armed for whoever first wires up event supersession — which, given
that `supersede` exists and is tested, is a thing the codebase intends to support.

### 2.3 What references an `events` row

| Reference | Points at | Effect of deleting the events row |
|---|---|---|
| `event_evidence.event_id` | `events.event_id` | deleted with it by `delete_event_reports` |
| `article_event_links.event_id` | `events.event_id` | deleted with it |
| `event_participants` / `event_instruments` / `event_sectors` | `events.event_id` | deleted with it |
| `canonical_event_reports.report_id` | `events.event_id` — *"The link, not a copy"* (`fusion_schema.py:77`) | **left dangling until the next fusion run rewrites the group** |
| `events.supersedes_event_id` | `events.event_id` | **the successor's pointer to its own history dangles** |
| `corpus_funnel.py:609` | `SELECT status, COUNT(*) FROM events GROUP BY 1` | a whole status category silently disappears from the corpus-health report |
| `research_observations.event_id` | `canonical_events.canonical_event_id` (`build_research_observations.py:149`) | **indirect, and the serious one — §2.4** |
| `event_studies.event_id` | `canonical_events.canonical_event_id` (`build_event_studies.py:222-235`) | **same indirect path** |

The last two are the ones that matter, and they matter because of how canonical ids are formed.

### 2.4 Demonstrated: deleting a retracted report dangles a research observation

`scripts/fuse_events.py:111-129` derives the canonical id as a content hash of the report set:

```python
def canonical_id_for(report_ids: List[str]) -> str:
    joined = "|".join(sorted(report_ids))
    return f"ce-{hashlib.sha1(joined.encode('utf-8')).hexdigest()[:16]}"
```

Fixture run (temporary database, created and deleted in the scratchpad):

```
PART 1 - does removing one report change its group's canonical id?
  canonical id, all 3 reports : ce-31d9b65951f20876
  canonical id, 2 reports     : ce-4413aa320e1a87cf
  SAME id?                    : False

PART 2 - two reports in one group, one RETRACTED, plus an observation on it
  canonical event before        : ce-62206c9179fc05ae
  research_observations.event_id: ce-62206c9179fc05ae
  LEFT JOIN resolves to         : ce-62206c9179fc05ae      <- healthy

  retention verdict  evt-1487c252ac56b26a  delete_superseded
  deleted                       : ['evt-1487c252ac56b26a']

  canonical id the next fusion run would mint: ce-5b69ed7de93c62f2
  equal to the stored one?                   : False
  canonical retention verdict                : ('ce-62206c9179fc05ae', 'delete_recomputed')

  research_observations.event_id still -> ce-62206c9179fc05ae
  LEFT JOIN now resolves to            : None              <- DANGLING
```

`scripts/compute_features.py:228-235` LEFT JOINs on exactly that reference, so
`event.confidence` and `event.independent_source_count` go NULL for that observation. **That is
the precise failure this whole repair exists to prevent, reintroduced by the repair's own
deletion rule.**

### 2.5 The finding is broader than terminal status, and it predates this change

Honesty requires stating the scope correctly: **every** report deletion has this consequence,
not only `delete_superseded`. An orphan, an article-gone row or a UUID remnant that belongs to a
canonical group churns that group's id in exactly the same way.

The difference is whether an alternative exists:

| Delete reason | Alternative to deletion? |
|---|---|
| `no_provenance` | none — the row can never be re-derived or verified |
| `article_gone` | none — same |
| `id_scheme_remnant` | none — the current writer can never mint or refresh it |
| **`superseded`** | **yes — keep the row and filter it from views, which `include_superseded` already does** |

So for three of the four the churn is an unavoidable cost of a necessary deletion. For the
fourth it is a self-inflicted cost of an unnecessary one.

And the underlying fragility is **pre-existing, not introduced here**. `fuse_events.py`'s own
docstring admits it (lines 121-126): *"if a later run attaches an additional report to a group,
the group's id changes."* A report **joining** a group churns the id just as a report leaving it
does. Three durable tables key off that id —
`canonical_events.canonical_event_id`, `research_observations.event_id` (and its derived
`observation_id_for(event_id, instrument_id)`), and `event_studies.event_id` (and its derived
`study_id_for(...)`).

**Inference, clearly labelled as such:** this is a plausible contributing mechanism for the
audit's 1,295-observations-against-129-canonical-events gap. Because
`build_research_observations` and `build_event_studies` mint ids by hashing the canonical id,
every churn creates a *new* observation and a *new* study while the old rows persist
(`INSERT OR REPLACE`, never deleted). That inflates observations relative to canonical events
in exactly the direction observed. I cannot verify it — confirming it needs the production
database, which this review may not open — and the audit's deletion-based explanation remains
sufficient on its own. But if both mechanisms are live, re-deriving events without first
stabilising the canonical id will make the gap worse, not better.

### 2.6 Recommendation for question 2

Remove the two branches that produce `DELETE_SUPERSEDED` from
`plan_event_report_retention` (the `TERMINAL_STATUSES` check and the `supersedes_event_id`
back-reference check) and let those rows fall through to the normal archived / out-of-scope /
unresolved handling. Keep `TERMINAL_STATUSES` as a constant if a future caller wants to
*filter* on it.

Justification in one line: the only two situations the rule covers are already handled correctly
by the rest of the policy, the domain model explicitly forbids destroying these rows, and
deleting them demonstrably breaks a historical relationship. **No code was changed in this
review.**

---

## 3. Test count

### 3.1 Exact counts from the working tree

Counted two independent ways — `unittest`'s own loader and `grep -c "^    def test_"` — which
agree exactly:

| File | `countTestCases()` | `def test_` | |
|---|---|---|---|
| `tests/data_access/test_article_archive.py` | 36 | 36 | ✓ |
| `tests/scripts/test_event_retention.py` | 44 | 44 | ✓ |
| `tests/scripts/test_compute_features_archive.py` | 21 | 21 | ✓ |
| **Total new tests** | **101** | **101** | |

### 3.2 The discrepancy

`docs/HISTORICAL_DATA_RETENTION_REPAIR_REPORT_2026_11_01.md:328` states:

> "**Totals: 185 new tests written, all passing; 796 tests run in the focused and package
> sweeps, 0 failures, 0 errors.**"

**"185 new tests" is wrong. The correct figure is 101.** The number is not reconcilable with any
grouping in the working tree: 185 is neither 101, nor 101 + 63 pre-existing (164), nor any
subtotal in the report's own §5. It appears to be an arithmetic slip in the summary line; every
per-file figure the report quotes (36, 44, 21) and every per-run figure (101, 63, 222, and the
six package totals) is correct.

**"796 tests run" is arithmetically right but loosely labelled.** It is exactly the sum of the
six package suites — 90 + 81 + 120 + 259 + 104 + 142 = 796 — and does not include the focused
runs, which overlap those packages anyway. "796 tests run in the package sweeps" would be
accurate.

### 3.3 The substantive claim survives

The error is in the count, not in the coverage. The report's §6 maps every required case to
named tests, and all of those tests exist and pass. Re-verified during this review:

```
tests.data_access.test_article_archive          36  OK
tests.scripts.test_event_retention              44  OK
tests.scripts.test_compute_features_archive     21  OK
```

---

## 4. Diff, working tree, and report-to-implementation agreement

### 4.1 Working tree

```
 M scripts/compute_features.py
 M scripts/fuse_events.py
 M scripts/populate_events.py
?? src/data_access/article_archive.py
?? src/events/retention.py
?? tests/data_access/test_article_archive.py
?? tests/scripts/test_compute_features_archive.py
?? tests/scripts/test_event_retention.py
?? docs/… (15 reports, this one included)

staged: 0
```

```
 scripts/compute_features.py | 175 +++++++++++++++++++++++++++++++++++++++++---
 scripts/fuse_events.py      |  41 ++++++++---
 scripts/populate_events.py  |  78 ++++++++++++++++----
 3 files changed, 257 insertions(+), 37 deletions(-)
```

Matches the implementation report's §4.3 exactly.

### 4.2 No unrelated file was modified

`git status --porcelain` over `src/modeling`, `src/features`, `scripts/train_models.py`,
`src/data_access/model_schema.py` and `research/` returns **0 lines**. The boundaries the task
set around model algorithms, feature-selection policy, cluster logic, the split, the embargo,
the holdout, the qualification thresholds and D20 are all intact — none of those files is in
the diff.

New code writes nothing to `articles` or `article_entities`: a grep for
`DELETE FROM articles`, `UPDATE articles`, `INSERT INTO articles` and
`DELETE FROM article_entities` across `src/events/retention.py` and
`src/data_access/article_archive.py` returns nothing.

### 4.3 Claims spot-checked against the code

| Report claim | Verdict |
|---|---|
| `ArchivedArticle` keeps four scalars, asserted by a test | **true** |
| UUID-remnant check runs *before* the archive rule | **true** — branch order in `plan_event_report_retention` |
| `scanned_article_ids=None` means "scanned nothing" | **true** — `scanned = … if … is not None else set()` |
| `loaded_report_ids=None` reproduces the old canonical behaviour | **true** — the `unexamined` filter is skipped |
| ~700 reports and ~450 canonical events already deleted | **true** — 846−153 = 693, 581−129 = 452 |
| archive: 35 files, 24 MB, 2023-10 → 2026-09, tracked in git | **true** |
| `reduce_database_size.py` not implicated | **true** — prunes only `raw_articles`, as its docstring states |
| "no schema change, no migration, no table rebuild" | **true** |
| "**no DDL at all**" | **imprecise.** The new modules contain no DDL, but both scripts still call the pre-existing, unchanged, idempotent `initialize_*_schema` helpers, which do execute `CREATE TABLE IF NOT EXISTS`. The accurate claim is "no new or changed DDL" |
| "185 new tests written" | **false — 101** (§3) |
| "`ArticleArchive` already supplies … `title`/`summary`" | **false** (§1) |

Three reporting errors in a 539-line report, two of them in summary sentences rather than in the
evidence sections. The implementation itself matches the report everywhere I checked except
where the report describes a capability the reader does not have (§1).

---

## 5. Risks

1. **`DELETE_SUPERSEDED` is a latent data-loss path.** Nothing sets a terminal status today, so
   it deletes nothing now. It fires the moment event supersession is wired up — and
   `EventRepository.supersede` exists, is documented and is tested, so that is an intended
   future state, not a hypothetical.
2. **Canonical-id churn breaks durable references, for any report deletion.** Not introduced by
   this change and acknowledged in `fuse_events.py`'s own docstring, but this change makes the
   churn reachable from a new direction and the planned event re-derivation would make it
   routine. Three tables and two derived id functions depend on an id that is a hash of a
   mutable set.
3. **Re-derivation would currently make things worse before better.** Recreating ~700 reports
   adds them back to existing groups, churning every affected canonical id, minting new
   observations and studies, and leaving the old ones dangling. The report's "the work is small"
   does not account for this.
4. **Memory headroom on the feature path is real but unmeasured in production.** 54,149 distinct
   archived ids × four scalars is single-digit MB today. A `fetch_full()` carrying title and
   summary would add ~10 MB of strings (15-20 MB resident). Both are fine on a GitHub runner;
   neither is capped.
5. **Derived tables will grow now that archived-source rows are preserved.** The
   `--max-db-mb 1400` guards remain, so the failure mode is a loud refusal. Steady-state size
   is unknown.
6. **Unverifiable here:** the actual post-repair feature coverage, the real deletion and
   preservation counts on the production corpus, and whether id churn has already produced
   orphaned observations. All three need either the production database or a pipeline run, and
   this review may do neither.

---

## 6. Final status and recommended next action

**TWO DEFECTS CONFIRMED, ONE REPORTING ERROR CONFIRMED — CHANGE NOT SAFE TO COMMIT AS IS**

The core of the repair is sound and well tested: the sweeps no longer delete on absence, the
archive is read back into feature computation, missing values stay missing, `--limit`/`--since`
no longer judge what they did not examine, and 101 new tests pass alongside every pre-existing
test that touches the modified code. But the `DELETE_SUPERSEDED` rule contradicts an explicit
invariant of the domain model and demonstrably dangles a research observation's event reference,
and the report overstates both the archive reader's capability and its own test count.

### Recommended next action

**Remove the `DELETE_SUPERSEDED` verdict from `src/events/retention.py`, correct the two report
claims, and only then take up event re-derivation — starting with the stability of the canonical
event id, not with the archive reader.**

In that order, because the order is the finding. Dropping the terminal-status rule is a few lines
and closes a latent data-loss path that the domain model already told us not to open. Correcting
the report costs nothing and stops a false capability claim from being planned against. And
re-derivation is not blocked by the missing `title`/`summary` — that part genuinely is small —
it is blocked by `canonical_id_for` hashing a mutable set while three durable tables key off the
result. Fix the identity first (a stable canonical key, or a persisted old-id → new-id mapping
that `research_observations` and `event_studies` can be migrated through), then re-derive. Done
the other way round, re-derivation would dangle more observations than the original deletion
ever did.
