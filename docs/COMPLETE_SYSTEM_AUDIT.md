# Complete system audit — Phase 17

Date: 2026-09-04 · Commit at audit start: `c8228e3` · Scope: Phases 0–16

This document records what the repository **actually is**, established by
inspecting it rather than by reading its own documentation. Where the
code and the docs disagreed, the code won and the docs were corrected.

---

## The headline finding

**The repository contains two systems that barely touch each other.**

| | System A — news & recommendations | System B — quant & execution |
|---|---|---|
| Phases | 1–9 | 10–16 |
| Location | flat modules in `src/*.py` | packages `src/<domain>/` |
| Import style | `from news_database import …` | `from src.domain.models import …` |
| Entry point | `run_daily.py` | 15 manual scripts + 3 CLIs |
| Schedule | **3× per day, automatic** | manual, except one weekday paper session |
| Data produced | 48,392 articles · 22,725 recommendations | 5 signals · 5 predictions · 2 models · **0 orders** |
| Consumers | the dashboard | the dashboard, weakly |

`run_daily.py` — the only thing that runs on a schedule — imports **27
modules, all of them from the flat legacy layer**. It touches none of
`src/domain`, `src/signals`, `src/portfolio`, `src/execution`,
`src/paper`, `src/backtest`, `src/modeling`, `src/research` or
`src/features`.

This is not a defect to be fixed by deleting one of them. System A
produces the data System B is designed to consume, and System B is the
architecture the project is heading toward. But **the joint between
them is manual**, and that explains nearly every other observation in
this audit: the empty tables, the unexercised code paths, the single
digit signal count.

---

## 1. Repository state

```
src/      171 python files    (55 flat legacy + 116 in 17 packages)
tests/    131 python files    2,815 tests
scripts/   22 python files
docs/       7 files
.github/   23 workflows
data/     marketlens.db — 82 tables, 38 of them empty
```

Real (non-bot) commits: 24. The other 470 are automated data-refresh
commits from the daily pipeline.

**Housekeeping:** a stale git worktree sits at
`.claude/worktrees/blissful-shaw-4e73ab`, pinned to commit `c3256a3`
(Phase 13) on an abandoned branch. It is excluded from git status but
**not** from `grep`, and it produced a false positive in this audit's
own MT5 search before being identified. Recommended: remove.

## 2. What actually runs automatically

| Workflow | Trigger | Reality |
|---|---|---|
| `daily` | cron 3×/day | the only production pipeline |
| `archive_articles` | cron daily | retention |
| `run_paper_session` | cron weekdays 21:30 UTC | runs, produces nothing — every signal is suppressed |
| `tests` | push + PR | full suite on Python 3.11 |
| **19 others** | **manual only** | including every Phase 4–12 stage |

The Phase 4–10 pipeline — canonical news ingestion, entity resolution,
event fusion, feature computation, model training, signal generation —
runs **only when a human clicks a button**. That is the direct cause of
the row counts in §4.

## 3. Sources of truth (spec §5)

Three domains have **two competing representations**, and in two of
them the better-designed one is the empty one.

| Domain | Legacy (populated) | Canonical (empty) | Verdict |
|---|---|---|---|
| Article | `articles` — 16 cols, 48,392 rows | `news_articles` — 27 cols, **48,392 rows** | **MIGRATED (TD-02).** All rows backfilled and verified; `articles` untouched and still read by seven modules, so it remains the write path pending TD-02b. |
| Trade idea | `recommendations` — 9 cols, 22,725 rows | `signals` — 40 cols, **5 rows** | **DUPLICATE.** `signals` carries instrument_id, model linkage, horizon, confidence, lifecycle. `recommendations` carries a ticker string. |
| Event | `events` — 846 rows | `canonical_events` — 581 rows | **NOT a duplicate.** This is the intended two-layer model: an event *report* (what one article claimed) versus a *canonical* event (what actually happened, corroborated). Correct as designed. |

Every other domain has exactly one source of truth. Entity identity is
properly modelled — `companies` / `securities` / `instruments` /
`exchanges` with stable ids, and ticker is **not** used as identity
(spec §11 satisfied).

## 4. Empty tables — 38 of 82

Grouped by cause, because they mean different things:

- **Never wired to production (14):** all `backtest_*`, `paper_*` except
  accounts/sessions, `simulated_orders`, `simulated_fills`. The code
  works — Phase 12 and 13 tests pass — but no scheduled job writes here.
- **Blocked upstream (5):** `order_intents`, `portfolios`, `positions`,
  `risk_decisions`, `risk_violations`. Nothing produces them because
  every signal is suppressed and no portfolio exists.
- **Superseded (1):** `raw_articles` — the legacy pipeline never kept
  provider payloads, so there is nothing to backfill. `news_articles`
  was in this group and now holds 48,392 rows (TD-02).
- **Genuinely unused features (6):** `event_instruments`,
  `event_sectors`, `fusion_contradictions`, `event_corrections`,
  `ingestion_checkpoints`, `allocation_*`.
- **Absent entirely:** the 14 Phase 14 execution tables and 13 Phase 16
  governance tables do not exist in the production database at all —
  they are created on first CLI run and no CLI has been run against it.

## 5. Time model (spec §7)

24 distinct timestamp column names across 82 tables. The important ones
are correctly **separated rather than collapsed**:

`event_time` · `publication_time` · `ingestion_time` · `detection_time`
are four distinct columns on `events`. Execution keeps `intent_at`,
`validated_at`, `submitted_at`, `acknowledged_at`, `terminal_at`,
`filled_at` apart. Trade outcomes keep decision price, submitted price
and fill price apart, which is the price-side equivalent.

No component was found mixing these concepts. **Spec §7: PASS.**

## 6. Point-in-time safety (spec §8)

`src/pointintime/view.py` implements a structural barrier rather than a
convention: a `PointInTimeView` anchored at moment T raises
`LookAheadViolation` on any read of data timestamped after T. The
docstring states the reasoning plainly — *"discipline fails silently the
first time someone forgets"*.

This is the strongest single design decision in the repository. Code
that leaks future data crashes in tests rather than producing
plausible-looking wrong numbers.

**Spec §8/§9: PASS**, with the caveat that the guard protects the
research path (Phases 6–10) and the legacy Phase 1–9 scoring engines
predate it and read full current history — harmless for a dashboard,
which is all they feed.

## 7. Execution safety (spec §25, §65)

Full call graph in `EXECUTION_SAFETY_AUDIT.md`. Summary:

- Exactly **one** call site reaches a venue: `orchestrator.py:549`.
- Gate order is correct: `safety.assert_not_real_money()` → validation
  (23 checks) → state machine → `_submit`.
- `risk_approved is None` **fails closed** — "not consulted" is not
  approval.
- **Finding (CRITICAL, now fixed):** the only producers of
  `risk_approved` were two CLI flags named `--assume-risk-approved`.
  The Phase 11 risk engine's output reached the database and stopped.
  See §9.

## 8. Dead code (spec §38)

**Zero unreachable modules.** A reachability analysis from all 24 entry
points initially reported three; all three were false positives caused
by the analyser not handling `from package import module`. Spec §38
warns about exactly this, and it caught the analyser rather than the
code.

**Nine modules are reached only by their tests** — built, tested, never
run in production:

| Module | Lines | Assessment |
|---|---|---|
| `src/research/builder.py` | 398 | **DUPLICATE** — `scripts/build_research_observations.py` (355 lines) reimplements it inline |
| `src/fusion/clustering.py` | 261 | Not imported by `fusion/engine.py`, which uses blocking/scoring/corroboration only |
| `src/data_access/entity_repository.py` + `entity_schema.py` | — | `scripts/backfill_article_entities.py` writes its own SQL instead |
| `src/entities/relationships.py` | — | no consumer |
| `src/api_collector.py`, `src/web_scraper.py` | — | Phase 2 collectors superseded by RSS/Finnhub/AlphaVantage |
| `src/check_registry_collisions.py` | — | **not dead** — run as a CI step in `tests.yml` |

The pattern is consistent and worth naming: **several scripts
reimplement logic that also exists as a library module.** That is a
domain-boundary violation (spec §41) as much as a duplication one.

## 9. The change this phase made

One architectural gap was closed. Everything else in this audit is
documented rather than altered, per spec §1 and §58.

**`src/execution/intake.py` (new).** The join between Phase 11 risk and
Phase 14 execution. `from_decision()` converts an approved
`RiskDecision` plus its `OrderIntent`s into `IntentRequest`s, taking
`risk_approved` from the decision **object** rather than from a
caller's claim. It raises `RiskNotApproved` on any non-approving state
and `LineageIncomplete` when an intent cannot be traced back to a
signal. It has no override parameter, and a test asserts that absence
so one cannot be quietly added back.

Why this and nothing else: the paper stack (Phase 13) already consulted
the real risk engine, and the broker stack did not. The asymmetry ran
the wrong way — the path that can reach a venue was the one taking risk
approval on trust.

## 10. Verdict

See `PRODUCTION_READINESS.md` for the graded assessment and
`TECHNICAL_DEBT_REGISTER.md` for the prioritised list.

Short version: the execution, risk and research layers are of
noticeably higher quality than the pipeline that connects them. The
system is **ready for paper trading** and structurally incapable of
real-money execution, which is the correct state. What stands between
it and usefulness is not safety and not architecture — it is that the
signal layer emits nothing, and has emitted nothing for five phases.
