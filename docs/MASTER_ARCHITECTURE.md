# Master architecture — as built, Phase 17

Reconstructed from the repository, not from the phase specifications.
Where the two disagreed, this document follows the code.

---

## 1. The shape of the system

```
 ┌─ SYSTEM A ─ news & recommendations ─ Phases 1-9 ─ RUNS 3x DAILY ────┐
 │                                                                     │
 │  RSS / Finnhub / AlphaVantage                                       │
 │        │                                                            │
 │  pipeline_core.process_articles   clean → dedupe → score            │
 │        │                                                            │
 │  NewsDatabase  →  articles (48,392)                                 │
 │        │                                                            │
 │  ConfidenceEngine → RecommendationEngine → recommendations (22,725) │
 │        │                                                            │
 │  BacktestEngine (check past recs) · RiskScoreCalculator             │
 │  PortfolioSimulator · SectorAggregator · DailySummary               │
 │        │                                                            │
 └────────┼────────────────────────────────────────────────────────────┘
          │
          ▼
      DashboardGenerator ──────────────► docs/index.html  (static, GitHub Pages)
          ▲
          │
 ┌────────┼─ SYSTEM B ─ quant & execution ─ Phases 10-16 ─ MANUAL ─────┐
 │                                                                     │
 │  news_articles(0) ─ entities ─ events(846) ─ canonical_events(581)  │
 │        │                                                            │
 │  PointInTimeView  ◄── the look-ahead barrier                        │
 │        │                                                            │
 │  research_observations(581) → research_features(19,103)             │
 │                             → research_labels(9,192)                │
 │        │                                                            │
 │  ModelingEngine → trained_models(2) → predictions(5)                │
 │        │                                                            │
 │  SignalEngine → signals(5)   [15 suppressions]                      │
 │        │                                                            │
 │  PortfolioService.evaluate() → RiskDecision → OrderIntent(0)        │
 │        │                                                            │
 │        ├──────────────► PaperSession → PaperExecutor  (Phase 13)    │
 │        │                                                            │
 │        └──► intake.from_decision()  ◄── ADDED PHASE 17              │
 │                    │                                                │
 │              IntentRequest                                          │
 │                    │                                                │
 │              ExecutionService  (Caller permissions)                 │
 │                    │                                                │
 │              ExecutionOrchestrator                                  │
 │                    ├─ ExecutionSafety.assert_not_real_money() ◄ HARD│
 │                    ├─ Validator (23 checks)                         │
 │                    ├─ RiskGovernor (23 limits)      (Phase 16)      │
 │                    ├─ TradingSession (preflight, frozen config)     │
 │                    └─ BrokerGateway ──► IBKRGateway ──► PAPER ONLY  │
 │                                                                     │
 └─────────────────────────────────────────────────────────────────────┘
```

**The joint marked `▲` is the whole story.** System A runs three times
a day and produces everything the dashboard shows. System B is the
architecture the project is heading toward and runs when a human clicks
a button.

## 2. Dependency direction

Strictly downward, verified: no module in `src/domain` imports from
`src/execution`; no module above the broker boundary imports from
`src/execution/adapters`; the orchestrator contains no
`if broker_id == "ibkr"`.

```
domain/          ← imported by everything, imports nothing of ours
data_access/     ← imports domain
<engines>/       ← import domain + data_access
execution/       ← imports domain + data_access
execution/adapters/ ← imports execution (never the reverse)
scripts/         ← import everything; imported by nothing
```

The one violation: **scripts contain business logic** that duplicates
library modules (TD-06).

## 3. Source of truth per domain

| Domain | Canonical | Status |
|---|---|---|
| Company / Security / Instrument / Exchange | `companies` / `securities` / `instruments` / `exchanges` | **Clean.** Stable ids; ticker is not identity |
| News article | `articles` (de facto) / `news_articles` (de jure) | **Split** — TD-02 |
| Event report | `events` | Clean |
| Canonical event | `canonical_events` | Clean — two layers by design, not duplication |
| Feature | `research_features` | Clean, versioned |
| Label | `research_labels` | Clean |
| Model | `trained_models` | Clean, versioned |
| Prediction | `predictions` | Clean |
| Trade idea | `signals` (de jure) / `recommendations` (de facto) | **Split** — TD-03 |
| Portfolio / Position | `portfolios` / `positions` | Clean, empty |
| Risk decision | `risk_decisions` | Clean, empty |
| Order intent | `order_intents` | Clean, empty |
| Execution order | `execution_orders` | Clean; table absent from prod DB |
| Fill | `execution_fills` | Clean; deduped on broker execution id |
| Trade outcome | `trade_outcomes` | Clean; 21-field flat lineage |

## 4. Ownership of state and logic

| Concern | Owner | Notes |
|---|---|---|
| What is true about the market | `price_candle_cache`, `articles` | write-once |
| What we knew at time T | `PointInTimeView` | structural barrier, raises |
| What we believe | `signals` | model-linked, lifecycle-managed |
| What we may do | `RiskDecision` | the only authority; execution refuses without one |
| What we did | `execution_orders` + `execution_fills` | idempotent, deduped |
| What it cost | `trade_outcomes` | decision / submitted / fill prices kept apart |
| Whether we may trade at all | `ExecutionSafety`, `ExecutionGovernor`, `TradingSession` | three independent gates |

## 5. Time

Four ingestion timestamps kept apart on every event: `event_time`,
`publication_time`, `ingestion_time`, `detection_time`. Execution keeps
six: `intent_at`, `validated_at`, `submitted_at`, `acknowledged_at`,
`terminal_at`, `filled_at`. Prices keep three: decision, submitted,
fill.

No component was found collapsing these. This is the second-strongest
design decision in the repository after the point-in-time barrier.

## 6. Subsystem classification (spec §69)

| Subsystem | Verdict | Why |
|---|---|---|
| Domain models (`src/domain`) | **KEEP** | 15 modules, canonical, well-documented, no duplication |
| Point-in-time (`src/pointintime`) | **KEEP** | structural barrier; the single best decision here |
| Execution (Phases 14–16) | **KEEP** | one choke point, gates in correct order, fails closed |
| IBKR adapter | **KEEP** | boundary held; zero IBKR types above it |
| Governance (Phase 16) | **KEEP** | levels, gates, four-eyes approval, sessions, limits |
| Risk engine (Phase 11) | **KEEP** | correct; was simply not connected |
| Risk → execution join | **ADD** ✔ | done — `src/execution/intake.py` |
| Paper trading (Phase 13) | **KEEP** | works, consults real risk; second lifecycle is TD-04 |
| Backtest (Phase 12) | **KEEP** | tested, unexercised on real data |
| Research / features / modeling | **KEEP** | point-in-time safe, versioned, unexercised |
| Fusion | **REFACTOR** | `clustering.py` unwired (TD-07) |
| `src/research/builder.py` | **MERGE** | script reimplements it (TD-06) |
| Entity repository | **MERGE** | script writes its own SQL (TD-06) |
| Legacy collectors | **DEPRECATE** | superseded (TD-08) |
| `news_articles` schema | **REFACTOR** | correct design, no producer (TD-02) |
| Legacy Phase 1–9 layer | **KEEP** | produces all live data; do not touch until B replaces it |
| Dashboard | **KEEP** | truthful — reports absence as absence |
| MT5 artifacts | **REMOVE** ✔ | done in Phase 16; only negative statements remain |

Nothing is classified **REWRITE**. No subsystem was found broken enough
to justify one, and spec §1 forbids rebuilding working systems.

## 7. What does not exist

Recorded so the audit is not read as having missed them:

- **No HTTP API.** No Flask, FastAPI, Django, aiohttp. The
  "API layer" is a typed Python facade (`ExecutionService`) with a
  `Caller` permission object.
- **No authentication or sessions.** Nothing is network-reachable.
  `Caller` defaults to read-only so an unconsidered caller cannot
  execute.
- **No realtime infrastructure.** No websockets, no queues, no workers.
  Scheduling is GitHub Actions cron; the dashboard is a static file.
- **No migration tool.** Schemas are additive
  `CREATE TABLE IF NOT EXISTS`, safe against a populated database.

Spec sections §32 (API), §35 (auth/CORS/CSRF), §42–44 (events, jobs,
realtime) are therefore assessed against what exists rather than
declared failed against infrastructure the project deliberately does
not have.
