# Technical debt register — Phase 17

Prioritised. Every item was observed in the repository, not inferred
from a document. Severity reflects **risk to correctness or capital**,
not effort.

Status key: `OPEN` · `FIXED` (in this phase) · `ACCEPTED` (understood,
deliberately not fixed).

---

## CRITICAL

### TD-01 — Risk decisions could not reach execution · **FIXED**

**Component:** `src/execution/orchestrator.py`, `scripts/run_*.py`

The only producers of `risk_approved=True` were two CLI flags named
`--assume-risk-approved`. `RiskDecision` objects from the Phase 11
engine reached the database and the dashboard and stopped there. The
validator correctly refused to trade without a verdict, so the only way
to obtain an order was for a human to assert one.

**Risk:** the path that can reach a broker took risk approval on trust,
while the paper path — which cannot reach a broker — consulted the real
engine. The asymmetry ran the wrong way.

**Action taken:** added `src/execution/intake.py`. `risk_approved` is
now a fact about a `RiskDecision` object. No override parameter exists,
and a test asserts that absence. 20 tests, including one that drives a
real IBKR paper order from an approved decision end to end.

---

## HIGH

### TD-02 — Two competing article schemas · **MIGRATED**

**Component:** `articles` (48,392 rows) → `news_articles` (48,392 rows)

The canonical schema was the better one — provider, canonical URL,
dedup levels, processing state — and it was empty. The Phase 2 domain
model had been written *expecting* this migration:
`NormalizedArticle` carries `sentiment_label`, `sentiment_score` and
`impact_score` with a comment saying they exist so migrated articles
keep what the legacy engines already produced. The design anticipated
it; nobody ran it.

**Done:** `scripts/migrate_news_to_canonical.py`. All 48,392 rows
backfilled and verified:

| Check | Result |
|---|---|
| legacy rows with a title | 48,392 |
| canonical rows | 48,392 |
| legacy rows not migrated | **0** |
| `duplicate_of` pointing nowhere | **0** |
| sentiment carried across | 48,392 |
| Phase 5 entity links still resolvable | **26,400 / 26,400** |
| `articles` table modified | **no** — SHA-256 identical before and after |

Wired into `daily.yml` after the pipeline and before archiving, with
`continue-on-error`: the script is idempotent, so a missed day
self-heals, whereas a red daily pipeline stops collecting news and
loses data permanently.

**What was deliberately not done.** `articles` was not dropped, not
altered, not read-only-ified. It holds the only copy of the legacy rows
and seven modules still read it. Retiring it is a separate decision
taken after those readers are repointed — see TD-02b.

**Consequence worth knowing:** the two tables now diverge in retention
by design. `archive_old_articles.py` prunes `articles` to 60 days;
nothing prunes `news_articles`. Canonical accumulates the full history,
which is what spec §50 asks for and what makes Phase 5 entity links
survive archiving — previously they dangled.

### TD-02b — Seven readers still on the legacy table · **OPEN**

`dashboard.py`, `impact_engine.py`, `news_database.py`,
`archive_old_articles.py`, `backfill_article_entities.py`,
`compute_features.py`, `populate_events.py` all read `articles`.

Until they are repointed, `articles` remains the write path and
`news_articles` is a synchronised projection of it. That is strictly
better than two independent definitions, one of them empty — but it is
not yet one source of truth.

**Recommended:** repoint readers one at a time, dashboard last (it is
the only user-visible one). Then make ingestion write canonical
natively and demote `articles`.

**Why ingestion was not repointed now:** `run_daily.py` puts only
`src/` on `sys.path`, not the repository root, so no flat Phase 1–9
module can import `src.domain.*`. Making the only scheduled production
job depend on the package layer is a larger change than a backfill
warrants, and it belongs with the reader migration above.

### TD-03 — Two competing trade-idea schemas · **OPEN**

**Component:** `recommendations` (22,725 rows) vs `signals` (5 rows)

`signals` carries instrument_id, model linkage, horizon, confidence,
probability, lifecycle, suppression. `recommendations` carries a ticker
string and a confidence score. The dashboard reads both.

**Risk:** the same conceptual object with two schemas and two
lifecycles. A user reading the dashboard cannot tell which is
authoritative.

**Recommended:** declare `signals` canonical. Keep `recommendations`
for its history. Label them distinctly in the UI.

### TD-04 — Two order lifecycles · **ACCEPTED**

**Component:** `src/paper/executor.py` vs `src/execution/orchestrator.py`

Phase 13's paper path does not pass through the Phase 14 orchestrator,
validator or safety layer. It is not a bypass — `PaperExecutor` cannot
reach a broker and `PaperAccount.is_paper` cannot be `False` — but it
is a second implementation of order lifecycle, fills and position
accounting.

**Risk:** a rule fixed in one path will not appear in the other.
Already visible: risk wiring existed only in the paper path until TD-01
was fixed, and idempotency handling differs between them.

**Accepted because:** merging them is a large change to two working,
well-tested subsystems, and the spec (§1, §59) prefers incremental
refactoring over rewriting working systems. Revisit when paper trading
actually produces orders.

### TD-05 — The Phase 4–12 pipeline is manual · **FIXED**

**Component:** 19 of 23 GitHub Actions workflows

Canonical news ingestion, entity backfill, event population, fusion,
price caching, feature computation, model training, signal generation,
research observations, event studies, backtests — **none is
scheduled**. They run when a human clicks.

**Risk:** this is the direct cause of 5 signals, 5 predictions, 2
models and 38 empty tables. The architecture is sound and unexercised.

**Done:** `.github/workflows/pipeline.yml` runs all eleven stages in
dependency order, on two cadences:

- **Sunday 02:00 UTC** — the full chain including the price cache
  (~2h30m)
- **Wednesday 02:00 UTC** — refresh without the price cache (~3 min),
  because predictions expire after 7 days and a weekly-only cadence
  would leave signals at the limit right before every run

02:00 UTC is deliberate: the daily pipeline runs at 13:00, 16:30 and
21:15, and even the long variant finishes around 04:40. `daily.yml`
records a measured incident where a 2h38m research job caused the daily
pipeline to be *skipped entirely for nearly 20 hours*.

One download and one upload for the whole chain, rather than eleven of
each — every download/upload pair is a chance for a concurrent run to
overwrite another's result.

Every stage is `continue-on-error` with an `if: always()` upload: the
scripts are idempotent and additive, so partial progress is safe and
the next run resumes. A run summary reports each stage's outcome so a
partial run does not read as a clean one.

Rehearsed locally against a copy of production before shipping: all
nine writing stages plus the dashboard, 176 seconds end to end.

---

## MEDIUM

### TD-06 — Scripts reimplement library modules · **OPEN**

| Script | Reimplements | Lines duplicated |
|---|---|---|
| `scripts/build_research_observations.py` | `src/research/builder.py` | ~355 vs 398 |
| `scripts/backfill_article_entities.py` | `src/data_access/entity_repository.py` | own SQL |

Both library modules are reached only by their tests. This is a
domain-boundary violation (spec §41) as much as duplication (§39):
business logic lives in scripts rather than services.

**Recommended:** make the scripts thin CLIs over the library modules.
Delete neither until the script is proven to produce identical output.

### TD-07 — `src/fusion/clustering.py` is not used by the fusion engine · **OPEN**

261 lines, imported only by its test. `fusion/engine.py` imports
`blocking`, `scoring` and `corroboration` but not `clustering`.

**Recommended:** determine whether the engine *should* cluster. If yes,
wire it. If no, remove with a note. Do not delete on the strength of
static analysis alone (spec §58).

### TD-08 — Legacy Phase 2 collectors superseded · **OPEN**

`src/api_collector.py` and `src/web_scraper.py` are reached only by
their tests. `run_daily.py` uses RSS, Finnhub and AlphaVantage
collectors instead.

**Recommended:** `DEPRECATE` — mark in the module docstring, keep the
tests, remove after one more phase without a consumer.

### TD-09 — Six genuinely unused feature tables · **OPEN**

`event_instruments`, `event_sectors`, `fusion_contradictions`,
`event_corrections`, `ingestion_checkpoints`, `allocation_changes`,
`allocation_proposals`.

Each represents a designed capability with no producer.
`fusion_contradictions` is notable: the fusion engine models
corroboration but never records a contradiction, which is the more
interesting signal.

**Recommended:** individually decide `wire` or `drop`. Schema-only, so
no data is at risk either way.

### TD-10 — Two import conventions · **ACCEPTED**

Flat (`from news_database import NewsDatabase`) for Phases 1–9,
package (`from src.domain.models import …`) for Phases 10–16. Tests
must run with `PYTHONPATH=src`, and running with `PYTHONPATH=.`
produces 13 import errors that look like failures.

**Accepted because:** normalising means touching 55 modules and 494
commits of history for a cosmetic gain. The cost is one line of
documentation, now present in `README` and CI.

---

## LOW

### TD-15 — `archive_old_articles.py` ignores its db argument when writing · **OPEN**

The script accepts a database path but writes archives to a hardcoded
`REPO_ROOT/data/archives/` (line 49). Running it against a copy of the
database — the obvious way to test it safely — silently modifies the
real archive files.

Found the hard way during the TD-02 migration: a test run against a
throwaway copy appended to the live `articles_2026-07.jsonl.gz`. No
data was lost (the change was reverted from git), but "run it on a
copy first" is the standard safety habit and this script defeats it.

**Recommended:** derive the archive directory from the database path,
or add `--archive-dir`. Low effort, and it restores the ability to
rehearse a destructive script safely.

### TD-11 — Stale git worktree · **OPEN**

`.claude/worktrees/blissful-shaw-4e73ab` pinned at `c3256a3` (Phase
13), abandoned branch. Excluded from `git status` but **not** from
`grep` — it produced a false positive in this audit's own MT5 search.

**Recommended:** `git worktree remove`.

### TD-12 — `requirements.txt` is unpinned · **FIXED**

Three packages, no version constraints: `feedparser`, `pandas`,
`yfinance`. CI installed `pandas 3.0.5` during this phase.

**Risk:** low today, real later — a pandas major version can change
`.iloc` and NaN semantics under a research pipeline whose whole value
is reproducibility.

**Done** (commit `f1c0868`): pinned to exactly what an unpinned install
resolved to on 2026-09-04 — `feedparser==6.0.14`, `pandas==3.0.5`,
`yfinance==1.7.0` — so pinning changed nothing that ran that day. The
file carries the rule for moving one: its own commit, full suite, and a
message saying what changed and why.

### TD-13 — CI runs Python 3.11, development is 3.12 · **OPEN**

Low risk, easily fixed, worth aligning before it hides a real
incompatibility.

### TD-14 — 13 tests need `pandas`/`feedparser` and say so unclearly · **FIXED (prior)**

Fixed earlier this session by installing the declared dependencies and
closing a leaked SQLite handle in three test seed helpers.

---

## Not debt — recorded so it is not "fixed" later by mistake

- **`events` vs `canonical_events`.** Not a duplicate. Event *report*
  (what one article claimed) versus canonical event (what happened,
  corroborated) is the intended Phase 6 model.
- **MT5 mentions in three documents.** All are negative statements
  recording the decision — *"there is no MT5 adapter"*. Removing them
  would delete the record of the decision.
- **`--assume-risk-approved`.** An operator override for hand-typed
  smoke tests, now explicitly labelled as one. Removing it would break
  legitimate manual verification.
- **The broker abstraction with one broker.** It keeps conids and IBKR
  status strings out of strategy and risk code, and makes a
  deterministic test double possible. It earns its place without a
  second venue.
