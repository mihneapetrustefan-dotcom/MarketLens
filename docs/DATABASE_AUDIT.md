# Database audit — Phase 17

Spec §30, §31, §50, §61. SQLite, single file, `data/marketlens.db`.

---

## 1. Inventory

**PRODUCTION: 47 tables, 42 populated, 5 empty.** (The 82/38 figure
carried here through Phase 17 came from a local development copy, not
from the release asset. Corrected in Phase 17.5.)

Historically stated as: 82 tables, 44 populated, 38 empty.** Plus 27 more (14 Phase 14
execution, 13 Phase 16 governance) that do not exist in the production
database at all — they are created on first CLI run, and no execution
CLI has ever been run against it.

Largest by row count:

```
  87,003  price_candle_cache
  48,392  articles
  26,400  article_entities
  22,725  recommendations
  19,103  research_features
   9,192  research_labels
   5,781  event_study_returns
```

## 2. The empty tables, by cause

*(38 was the local-copy figure through Phase 17. In production the
number is 5: `event_corrections`, `event_instruments`, `event_sectors`,
`ingestion_checkpoints`, `raw_articles`. The grouping below still
explains what each kind means.)*

The grouping matters — these mean different things:

| Cause | Count | Examples | Action |
|---|---|---|---|
| Never wired to a scheduled job | 14 | all `backtest_*`, most `paper_*` | schedule (TD-05) |
| Blocked upstream | 5 | `order_intents`, `portfolios`, `positions`, `risk_decisions`, `risk_violations` | unblocks when signals flow |
| Superseded schema | 2 | `news_articles`, `raw_articles` | migrate (TD-02) |
| Designed, never wired | 7 | `event_instruments`, `event_sectors`, `fusion_contradictions`, `event_corrections`, `ingestion_checkpoints`, `allocation_proposals`, `allocation_changes` | decide per table (TD-09) |
| Structural | 10 | `sqlite_sequence`, link tables | keep |

`fusion_contradictions` deserves a specific note: the fusion engine
models corroboration but never records a contradiction, which is the
more interesting signal of the two. Either wire it or remove it.

## 3. Duplicate schema (spec §5)

Two genuine duplications, both in `TECHNICAL_DEBT_REGISTER.md`:

- **`articles` (48,392) vs `news_articles` (0) + `raw_articles` (0)** —
  TD-02. The canonical schema is the better one and it is empty.
- **`recommendations` (22,725) vs `signals` (5)** — TD-03.

One apparent duplication that is **not** one: `events` (846) vs
`canonical_events` (581). Event *report* versus corroborated canonical
event is the intended Phase 6 two-layer model. Recorded here so it is
not "cleaned up" later by someone reading only the row counts.

## 4. Constraints and indexes

- Primary keys present on every table inspected.
- Unique constraints where they matter:
  `execution_orders.idempotency_key`, fill dedupe on broker execution
  id, `system_health_readings` composite key.
- `trade_outcomes` carries six indexes on exactly the columns a
  learning system would query by — `strategy_id`, `model_version`,
  `signal_id`, `instrument_id`, `market_regime`, `session_id`.
- Foreign keys are largely **declared but not enforced** — SQLite needs
  `PRAGMA foreign_keys=ON` per connection, and the repositories do not
  set it.

**Finding (MEDIUM):** FK enforcement is off. Orphan-record risk is real
but currently theoretical, since the tables that would orphan are
empty. Worth enabling before the pipeline is scheduled, not after.

## 5. Write safety

- Phase 14/16 schemas are additive `CREATE TABLE IF NOT EXISTS` — safe
  against a populated database. No `DROP` anywhere.
- `ON CONFLICT(order_id) DO UPDATE` on order persistence rather than
  `INSERT OR REPLACE`: the latter would silently delete an order that
  collided on its idempotency key. That was a real Phase 14 bug, found
  and fixed then.
- `INSERT OR IGNORE` on session events: history is append-only, and a
  re-save must not rewrite what was recorded.

## 6. Performance (spec §31)

No N+1 pattern found in the repository layer — reads are batched by
design. The heaviest operation measured is not a query: it is
`cache_price_candles.py`, which took **2h38m** in a recorded run and
caused the daily pipeline to be skipped for roughly 20 hours through a
shared GitHub Actions concurrency group. That incident is documented in
`daily.yml`'s own comments, and the group was split in response.

**The bottleneck is job scheduling, not SQL.**

Database size is actively managed: `archive_old_articles.py`
(`--keep-days 60`) and `reduce_database_size.py` (`--keep-days 14`) run
daily. That is why `articles` sits at 48k rather than growing without
bound.

## 7. Retention (spec §50)

| Class | Retained | Assessment |
|---|---|---|
| Raw articles | 60 days | reasonable; full text is re-fetchable |
| Price candles | indefinite | correct — the research substrate |
| Research features / labels | indefinite | correct — reproducibility |
| Models / predictions | indefinite | correct |
| Signals / outcomes | indefinite | correct |
| Orders / fills / audit | indefinite | correct — never prune an audit trail |
| Reconciliation | indefinite | correct |

**No retention policy deletes anything a future learning phase would
need.** Spec §50: PASS.

## 8. Migrations (spec §61)

No migration framework. Schema changes are additive
`CREATE TABLE IF NOT EXISTS`, plus `ALTER TABLE … ADD COLUMN` guarded
by a `PRAGMA table_info` check where a column was added.

Adequate for one SQLite file with one writer. It would not be adequate
for concurrent writers or for column removals, neither of which this
system performs.
