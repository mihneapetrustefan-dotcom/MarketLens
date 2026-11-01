# Corrective Patch — Historical Retention: Terminal Events and Report Accuracy

**Date:** 2026-11-01
**Repository:** `mihneapetrustefan-dotcom/MarketLens`
**Corrects:** `docs/HISTORICAL_DATA_RETENTION_REPAIR_REPORT_2026_11_01.md`
**On the findings of:** `docs/HISTORICAL_DATA_RETENTION_REVIEW_2026_11_01.md`
**Final status:** **PATCH APPLIED AND TESTED — NOT COMMITTED**

Terminal-status deletion removed, 7 regression tests added, 5 report statements corrected,
the mutable canonical-ID problem documented in the code as an unresolved architectural risk.
108 new tests pass; 266 tests in the `tests/scripts` package pass.

Nothing was committed, pushed or staged. No production database, backup or `db-latest` asset
was opened. No pipeline ran, no model was trained or promoted, no event was re-derived, no
broker integration touched, D20 unchanged. `canonical_id_for` was not modified and no
`research_observations` or `event_studies` row was migrated.

---

## 1. Objective and scope

Two things, and deliberately nothing else:

1. remove the confirmed terminal-event deletion defect from `src/events/retention.py`, with
   regression tests;
2. correct the inaccurate statements in the implementation report.

Explicitly **not** started: historical event re-derivation, any change to `canonical_id_for`,
and any migration of the tables that reference a canonical id. The canonical-ID problem is
*documented*, not fixed.

---

## 2. Root cause addressed

**Verified fact**, from the review and re-confirmed here.

The first version of `plan_event_report_retention` deleted an event report on two signals it
read as "positive evidence of invalidity":

```python
if status and str(status).lower() in TERMINAL_STATUSES:        # superseded/cancelled/retracted
    ... DELETE_SUPERSEDED
if event_id in superseded_by:                                  # named by another row's pointer
    ... DELETE_SUPERSEDED
```

Both readings were wrong, for two independent reasons.

**The domain model says the opposite.** `src/data_access/event_repository.py:221-234`:

> `supersede(...)`: "the new version points back at the old one, and the old one is marked
> SUPERSEDED **rather than deleted — historical states are never destroyed.**"

and the repository is built around *filtering* those rows, not removing them —
`list(include_superseded=False)` adds `e.status != 'superseded'` (lines 350-352) and
`count(include_superseded=False)` does the same (lines 408-412). A terminal status means **"no
longer current"**, never "safe to destroy".

**Deleting one measurably breaks a historical relationship.**
`scripts/fuse_events.canonical_id_for` hashes the *set of report ids* in a group, so removing a
report changes the group's canonical id. `research_observations.event_id` and
`event_studies.event_id` both reference that id and are never rewritten, and
`scripts/compute_features.py:232` LEFT JOINs on exactly it. The review demonstrated the full
chain on a fixture: delete a retracted report → canonical id changes
(`ce-62206c9179fc05ae` → `ce-5b69ed7de93c62f2`) → the stale canonical row is pruned as
recomputed → the observation's LEFT JOIN resolves to `None`. That is the NULL-feature failure
this entire body of work exists to prevent.

---

## 3. Decisions

### 3.1 Terminal rows are kept, and the keep is visible

`DELETE_SUPERSEDED` is gone. In its place, `KEEP_TERMINAL_HISTORY` — a *keep* verdict with its
own label, so a preserved terminal row is reported rather than silently absorbed:

```
KEEP_TERMINAL_HISTORY: "PASTRAT: stare istorica (inlocuit/anulat/retras)"
```

A keep verdict rather than no verdict at all, because an operator reading the retention summary
should be able to see that terminal rows exist and are being retained on purpose.

### 3.2 `TERMINAL_STATUSES` is kept — as a filtering constant

The constant stays, with its docstring rewritten to say what it is *not*:

> THIS IS A FILTERING CONSTANT, NOT A DELETION TRIGGER … It is kept because callers that want to
> EXCLUDE these rows from a view legitimately need the set, exactly as
> `EventRepository.list(include_superseded=False)` and `count(include_superseded=False)` do.

### 3.3 The pointer is read for reporting, never for deletion

`supersedes_event_id` is still collected, but only to annotate the decision detail
(`"status=retracted, superseded by evt-…"`). The comment at the collection site states why
following it to delete would be self-defeating:

> a successor's `supersedes_event_id` is a pointer INTO history, so following it to delete the
> predecessor would destroy the thing the pointer exists to reach.

A row named by a successor but carrying no terminal status of its own is also kept — same
verdict, different detail. That closes the case where `supersede()` has run but
`update_status` has not.

### 3.4 Ordering: provenance and the id scheme keep precedence

**Implementation decision, and the one place this patch diverges from the review's suggested
"let them fall through".** The terminal check sits **after** the provenance and id-scheme
checks and **before** the live/archive/scan checks:

| # | Condition | Verdict | Changed? |
|---|---|---|---|
| 1 | no `article_event_links` and no `event_evidence` | **DELETE** `no_provenance` | unchanged |
| 2 | `event_id != report_id_for(article_id, event_type)` | **DELETE** `id_scheme_remnant` | unchanged |
| 3 | `events.status` in `TERMINAL_STATUSES` | **KEEP** `terminal_history` | **was DELETE** |
| 4 | named by another row's `supersedes_event_id` | **KEEP** `terminal_history` | **was DELETE** |
| 5 | article live **and** scanned this run | **KEEP** `unresolved` | unchanged |
| 6 | article live, **not** scanned (`--limit`/`--since`) | **KEEP** `out_of_scan_scope` | unchanged |
| 7 | article absent from live, **in archive** | **KEEP** `archived_source` | unchanged |
| 8 | article in neither | **DELETE** `article_gone` | unchanged |

Why rules 1 and 2 keep precedence: a row with no resolvable article, or an id the current writer
can never mint, cannot be re-derived or verified *whatever its status says*. Letting a terminal
status override them would turn "retracted" into a way to immortalise an orphan, and the task
required the existing orphan and UUID-remnant cleanup to be preserved. Two tests pin this
(`test_terminal_status_does_not_immortalise_an_orphan`,
`test_terminal_status_does_not_immortalise_a_uuid_remnant`).

Why rule 3 precedes rule 8 — and where this diverges from the review: the review suggested
simply removing the two branches and letting terminal rows fall through to the normal handling.
Under that design, a terminal row whose article is gone from **both** the live table and the
archive would be deleted as `article_gone`. The task's instruction was explicit — *"Preserve
terminal events as historical records"* — and the domain invariant is unconditional, so the
terminal check is placed before rule 8 and such a row is kept.
`test_a_terminal_event_is_retained_even_with_no_archive` is exactly that case, and is the only
behavioural difference between this patch and a literal reading of the review.

### 3.5 Canonical identity: documented, not touched

`canonical_id_for` is unchanged. `plan_canonical_retention` is unchanged. No row in
`research_observations` or `event_studies` was read for writing or migrated.

What was added is a docstring section in `src/events/retention.py` headed
**"UNRESOLVED ARCHITECTURAL RISK: THE CANONICAL ID IS MUTABLE"**, which states the mechanism,
names the three dependent keys, and records the consequence for anyone planning re-derivation.
Full text in §7.

---

## 4. Files changed

| File | Change | Lines |
|---|---|---|
| `src/events/retention.py` | terminal deletion removed, `KEEP_TERMINAL_HISTORY` added, `TERMINAL_STATUSES` redocumented, canonical-ID risk documented | 386 → **471** |
| `tests/scripts/test_event_retention.py` | 3 tests replaced by 10; new class `TestTerminalStatusIsHistoryNotGarbage` | 807 → **969** |
| `docs/HISTORICAL_DATA_RETENTION_REPAIR_REPORT_2026_11_01.md` | 5 correction notices | 539 → **572** |

Unchanged, byte-for-byte — hashes match those recorded in the review:

```
src/data_access/article_archive.py              316 lines  f35ae95569252900
tests/data_access/test_article_archive.py       377 lines  f465705f6663ef6f
tests/scripts/test_compute_features_archive.py  407 lines  9a4c91cbd385b76b
```

**The patch touched no tracked file.** `src/events/retention.py` and
`tests/scripts/test_event_retention.py` are both still untracked, so `git diff --stat` is
byte-identical to before the patch:

```
 scripts/compute_features.py | 175 +++++++++++++++++++++++++++++++++++++++++---
 scripts/fuse_events.py      |  41 ++++++++---
 scripts/populate_events.py  |  78 ++++++++++++++++----
 3 files changed, 257 insertions(+), 37 deletions(-)
```

`scripts/populate_events.py` needed no edit: it consumes the policy through
`plan.to_delete`, `plan.to_keep`, `plan.unresolved` and `plan.summary_lines()`, none of which
changed shape. The new verdict appears in its printed summary automatically.

---

## 5. Report corrections applied

Five, each marked in place with a dated notice quoting what the original said. The review asked
for four; the fifth became necessary because changing the policy invalidated the report's own
fixture counts.

| # | Location | Was | Now |
|---|---|---|---|
| 1 | §5.1 totals | "185 new tests written … 796 tests run in the focused and package sweeps" | **101 new tests** … **796 tests run in the package-level sweeps**, with the arithmetic shown and a pointer to the current total of 108 |
| 2 | §10 recommendation | "The same `ArticleArchive` already supplies the four fields that query selects (`article_id`, `title`/`summary`, `source`, `published_at`)" | corrected: `ArchivedArticle` exposes four scalars and `fetch()` never reads `title` or `summary`; the fields are in the archive *files*; a second wider fetch path is required; the original list was five fields, not four |
| 3 | §9 boundaries | "No DDL at all" | "no schema change, no migration, no table rebuild", with the accurate claim **no new or changed DDL** and the note that the scripts still call the pre-existing `initialize_*_schema` helpers |
| 4 | §3.2 + §3.3 | terminal status described as "the data says so" and deleted in rules 1-2 | corrected to KEEP, with the `supersede` invariant quoted and the dangling-reference consequence stated; the verdict table re-ordered to match the code |
| 5 | §5.6 fixture counts | 15 deleted / 55 kept, "2 delete_superseded" | **13 deleted / 57 kept**, "2 keep_terminal_history", re-measured after the patch, with the pre-patch figures retained for comparison |

A sixth annotation marks §8 risk 1 as **superseded**: a retraction is now never deleted, not
merely spared when its article is live.

`docs/HISTORICAL_DATA_RETENTION_REVIEW_2026_11_01.md` was **not modified** — 447 lines, as
written.

---

## 6. Tests and exact results

All runs `PYTHONPATH=src python -m unittest …` from the repository root, against temporary
databases created per test. No production data, no network, no pipeline.

### 6.1 New tests in this patch — 7 net, 10 added, 3 removed

Class `TestTerminalStatusIsHistoryNotGarbage` in `tests/scripts/test_event_retention.py`:

| Test | Proves |
|---|---|
| `test_retracted_event_is_retained` | a retracted row survives, `to_delete` is empty |
| `test_superseded_and_cancelled_events_are_retained` | both other terminal statuses survive |
| `test_every_terminal_status_is_retained` | iterates `TERMINAL_STATUSES` itself, so a future status cannot quietly become deletable |
| `test_a_terminal_event_is_retained_even_with_no_archive` | the one case where the terminal rule and `article_gone` disagree (§3.4) |
| `test_a_successors_pointer_does_not_delete_its_predecessor` | the `supersedes_event_id` back-reference keeps rather than deletes, and the detail names the successor |
| `test_the_pointer_itself_survives_the_rebuild` | after the rebuild, `events JOIN events ON p.event_id = e.supersedes_event_id` still resolves |
| `test_terminal_status_does_not_immortalise_an_orphan` | provenance keeps precedence → still `DELETE_NO_PROVENANCE` |
| `test_terminal_status_does_not_immortalise_a_uuid_remnant` | id scheme keeps precedence → still `DELETE_ID_SCHEME_REMNANT` |
| `test_keep_terminal_history_is_not_a_delete_verdict` | the constant is outside `DELETE_VERDICTS` |
| **`test_a_historical_canonical_reference_still_resolves`** | **the regression end to end**: two reports in one group, one retracted, an observation on the group's canonical id; after retention the report survives, `canonical_id_for(surviving)` equals the stored id, and the observation's LEFT JOIN still resolves |

Removed, because they asserted the defective behaviour:
`test_retracted_status_is_positive_evidence_and_deletes`,
`test_superseded_and_cancelled_statuses_delete_too`,
`test_a_row_another_event_supersedes_is_deleted`.
Retained unchanged: `test_a_live_status_does_not_delete`.

### 6.2 Results

```
python -m unittest tests.scripts.test_event_retention
    Ran 51 tests in 17.224s                                       OK     (was 44)

python -m unittest tests.data_access.test_article_archive
    Ran 36 tests in 0.442s                                        OK     (unchanged)

python -m unittest tests.scripts.test_compute_features_archive
    Ran 21 tests in 8.603s                                        OK     (unchanged)
```

**New-test total: 36 + 51 + 21 = 108.**

### 6.3 All directly affected tests

Every test module importing any touched module
(`grep -rln "compute_features\|populate_events\|fuse_events\|article_archive\|events.retention\|archive_old_articles" tests/`):

```
tests.adaptive.test_recovery_boundary        tests.scripts.test_fuse_events
tests.data_access.test_article_archive       tests.scripts.test_populate_events
tests.scripts.test_compute_features          tests.test_dashboard_canonical_corpus
tests.scripts.test_compute_features_archive  tests.test_event_fusion
tests.scripts.test_event_retention           tests.test_news_database

    Ran 229 tests in 59.573s                                      OK     (was 222)
```

Package sweeps over the affected areas:

```
python -m unittest discover -s tests/events      -t .    Ran  81 tests in   0.127s   OK
python -m unittest discover -s tests/data_access -t .    Ran  90 tests in   5.090s   OK
python -m unittest discover -s tests/scripts     -t .    Ran 266 tests in 209.252s   OK
```

**0 failures, 0 errors anywhere.** The full 5,542-test suite was not run — the task asked for
focused tests and the directly affected ones, which are all above.

### 6.4 Static invariants, checked

```
python -m py_compile src/events/retention.py                       -> OK
grep -rn "DELETE_SUPERSEDED" src/ scripts/ tests/                  -> no hits
DELETE_VERDICTS: delete_article_gone, delete_dangling_reports,
                 delete_id_scheme_remnant, delete_no_provenance,
                 delete_recomputed                                 (5, was 6)
KEEP_TERMINAL_HISTORY in DELETE_VERDICTS                           -> False
TERMINAL_STATUSES: cancelled, retracted, superseded                -> still exported
every verdict has a VERDICT_LABELS entry                           -> True
```

### 6.5 Fixture re-measurement

Same 70-row fixture as the implementation report (session scratchpad, not in the repository),
re-run after the patch. One fixture bug was corrected while re-running: the two "retracted"
rows had been built with `report_id_for(aid, "retraction_case_N")` while storing
`event_type='acquisition'`, so the id-scheme rule claimed them before the terminal rule could.
They now carry an id the current scheme would mint, which is what makes them a genuine terminal
case.

| Policy | deletes | keeps | unresolved |
|---|---|---|---|
| old (`existing − produced`) | 70 of 70 — 100 % | 0 | — |
| new, **pre-patch** | 15 of 70 — 21.4 % | 55 | 10 |
| new, **post-patch** | **13 of 70 — 18.6 %** | **57 — 81.4 %** | **10** |

```
  40  keep_archived_source       PASTRAT: articolul sursa e arhivat
  10  keep_unresolved            PASTRAT NEREZOLVAT: articol viu, scanat, dar neregenerat
   6  delete_article_gone        articolul sursa lipseste si din live si din arhiva
   5  keep_out_of_scan_scope     PASTRAT: in afara domeniului scanat
   4  delete_id_scheme_remnant   id din schema veche (rest de migrare uuid4)
   3  delete_no_provenance       fara provenienta (nicio legatura la articol)
   2  keep_terminal_history      PASTRAT: stare istorica (inlocuit/anulat/retras)
```

Canonical side, unchanged by this patch: unrestricted run deletes 12 / keeps 0;
`--limit` with 3 of 12 reports loaded deletes 3 / keeps 9.

---

## 7. Unresolved canonical-ID risk

**Documented in code, not fixed.** `src/events/retention.py` now carries this, verbatim:

> `scripts/fuse_events.canonical_id_for` derives a canonical event's id by hashing the SET OF
> REPORT IDS it fuses. So the id changes whenever that set changes — when a report is deleted,
> and equally when a new report JOINS the group, which `fuse_events`' own docstring states: "if
> a later run attaches an additional report to a group, the group's id changes."
>
> Three durable things key off that id and are never rewritten:
>
>     canonical_events.canonical_event_id
>     research_observations.event_id      (and observation_id_for(...))
>     event_studies.event_id              (and study_id_for(...))
>
> So any change to a group's membership leaves those references pointing at an id that no longer
> exists. `compute_features.py:232` LEFT JOINs `research_observations` to `canonical_events` on
> it, and a dangling reference becomes NULL event metadata — the exact failure
> `docs/MODEL_CLUSTER_INSUFFICIENCY_AUDIT_2026_11_01.md` measured.
>
> CONSEQUENCE FOR ANYONE PLANNING EVENT RE-DERIVATION. Re-deriving the reports whose articles
> were archived would ADD them back to existing groups, churning the ids of every group they
> join. That must be solved — a stable canonical key, or a persisted old-id → new-id mapping the
> dependent tables can be read through — BEFORE re-derivation, or re-derivation will dangle more
> references than the original deletion ever did. Nothing in this module attempts it.

Status of the risk after this patch:

- **Reduced**, not removed. One class of deletion (terminal status) no longer churns ids. The
  three remaining delete verdicts still do, and for them there is no alternative — a row with no
  provenance, an unmintable id, or an article gone from both the live table and the archive
  cannot be re-derived or verified by any means available.
- **Still fully live for additions.** A new report joining a group churns the id just as a
  removal does, and nothing in this patch touches that.
- **Pre-existing and acknowledged upstream.** `fuse_events.py:121-126` documented the behaviour
  before any of this work began.
- **Unverified in production.** Whether churn has already orphaned observations cannot be
  checked without the production database, which these boundaries forbid. The audit's
  deletion-based explanation for the 1,295-observations-against-129-canonical-events gap remains
  sufficient on its own; id churn as a *contributing* mechanism stays an inference.

---

## 8. Remaining risks

1. **Terminal rows now accumulate in `events`.** Unbounded in principle, bounded in practice by
   the `--max-db-mb 1400` guards in both scripts, which refuse the write rather than corrupt
   anything. Nothing sets a terminal status today (§9), so growth is currently zero.
2. **The mutable canonical id** — §7. The single largest remaining architectural risk in this
   area.
3. **Re-derivation is still blocked**, now for two documented reasons: the archive reader does
   not expose `title`/`summary` (a small, well-understood gap), and the canonical id is not
   stable under a changing report set (the real blocker).
4. **A genuine retraction is now never removed by the sweep.** Removing it from a *view* is the
   repository's job through `include_superseded`. If a caller ever needs retracted reports gone
   from a derived table, that requires an explicit, separately-reviewed operation — not a
   rebuild side effect.
5. **The feature-path memory budget is unchanged** (four scalars per archived article, ~54,149
   distinct ids). A future `fetch_full()` for re-derivation would add ~10 MB of strings and
   should stay on its own path.
6. **Unverified in production**, as before: actual post-repair feature coverage, real deletion
   and preservation counts on the live corpus, and the `events`/`canonical_events` steady-state
   size.

---

## 9. Boundaries

| Boundary | Status |
|---|---|
| No production database, backups or `db-latest` access | **Honoured.** Only per-test temporary databases; no release read, no network call to GitHub |
| No production pipeline | **Honoured.** No workflow dispatched; the CLI entry points ran only against temp databases inside tests |
| No model training or promotion | **Honoured.** `train_models.py` and `promote_model.py` untouched and not run |
| No event re-derivation | **Honoured.** `load_articles_with_entities` is unchanged and still reads `FROM articles` |
| No broker integration | **Honoured.** Nothing touched |
| No D20 changes | **Honoured.** `research/protected_tests/ledger.jsonl` unchanged at 633 bytes; `git diff HEAD -- research/` empty |
| No commit or push | **Honoured.** Nothing staged, HEAD still `8c5ab63` |
| `canonical_id_for` unchanged | **Honoured.** `scripts/fuse_events.py` has no new modification; its tracked diff is byte-identical to before this patch |
| No `research_observations` / `event_studies` migration | **Honoured.** Written only inside one test's own temp database, as fixture setup |
| Review report not overwritten | **Honoured.** 447 lines, unmodified |
| No report overwritten | **Honoured.** This is a new file; the implementation report was *annotated in place* with dated notices, as instructed, and its original wording is quoted inside each notice |

Working tree:

```
 M scripts/compute_features.py        (unchanged by this patch)
 M scripts/fuse_events.py             (unchanged by this patch)
 M scripts/populate_events.py         (unchanged by this patch)
?? src/data_access/article_archive.py          (unchanged by this patch)
?? src/events/retention.py                     <- patched
?? tests/data_access/test_article_archive.py   (unchanged by this patch)
?? tests/scripts/test_compute_features_archive.py (unchanged by this patch)
?? tests/scripts/test_event_retention.py       <- patched
?? docs/… (16 reports, this one included)

staged: 0
```

---

## 10. Final status and recommended next action

**PATCH APPLIED AND TESTED — NOT COMMITTED**

The defect is closed. Terminal-status rows and rows named by a successor's
`supersedes_event_id` are retained as `KEEP_TERMINAL_HISTORY` and reported as such; the two
legitimate cleanups keep precedence over them, proven by test; the `supersedes_event_id` pointer
still resolves after a rebuild, proven by test; and a research observation's canonical reference
still resolves after retention, proven end to end by the test that reproduces the exact
regression the review demonstrated. `TERMINAL_STATUSES` survives as what it always should have
been — a filtering constant. All five inaccurate report statements are corrected in place with
their original wording quoted, and the mutable canonical ID is recorded in the code as an
unresolved architectural risk.

### Recommended next action

**Commit this work as one reviewable change — the archive reader, the narrowed retention policy,
the corrective patch and the three reports together — before anything else is built on it.**

It is currently eight untracked and modified files carrying a behavioural change to two pipeline
stages, validated only in a working tree that nothing else can see. The next piece of work on
this path is the canonical-ID stabilisation described in §7, and that is a change to how
`research_observations` and `event_studies` resolve their references — the kind of change that
needs a known-good baseline underneath it, not an uncommitted one.

Commit it, open a PR so CI runs the full 5,542-test suite against it rather than the focused
subset run here, and only then take up canonical-ID stability. Re-derivation comes after that,
not before.
