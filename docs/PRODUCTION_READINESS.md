# Production readiness — Phase 17

Spec §74, §75. Scores are 0–100 and every one is explained. They are
deliberately not inflated: a subsystem that is well built but never
exercised does not score as if it were proven.

---

## 1. Scorecard

| Domain | Score | Why exactly this |
|---|---|---|
| **Data** | 72 | 48k articles, 87k candles, active retention, clean entity identity. Held down by two competing article schemas (TD-02) and FK enforcement being off. |
| **Intelligence** | 55 | Event fusion, corroboration and credibility are properly modelled. `clustering.py` is unwired and `fusion_contradictions` is never written — the engine records agreement but not disagreement. |
| **Research** | 78 | `PointInTimeView` raises rather than filters; that single decision is worth more than most of the rest. Held down by 34 tests for the guarantee everything depends on. |
| **Quant** | 60 | 19k features, 9k labels, versioned and reproducible. Ran once, manually. |
| **Models** | 50 | Registry, versions, evaluations, baseline comparisons all exist. **Two models, five predictions.** The infrastructure is real; the evidence is not. |
| **Signals** | 35 | Engine, lifecycle, suppression, evaluation, outcome scoring — all built and tested. **5 signals, 15 suppressions.** The system suppresses more than it emits, and has for five phases. |
| **Portfolio** | 70 | Sizing, exposure, constraints, valuation, analytics. Correct and empty — zero portfolios exist. |
| **Risk** | 80 | Waterfall, hard/soft constraints, `INSUFFICIENT_DATA` as a first-class verdict, approval never the fallback. Now finally connected to execution. |
| **Backtest** | 68 | Replay, slippage, fees, partial fills, walk-forward, robustness. Never run against the production database. |
| **Paper trading** | 75 | Durable sessions, real risk engine, clock, freshness, health. Runs weekdays and produces nothing, because signals are suppressed. |
| **IBKR** | 72 | Boundary held, 12 error categories, conid resolution refuses ambiguity, 20 adversarial cases. **Never run against a real account** — the ceiling on this score. |
| **Execution** | 88 | One choke point, gates in the right order, fails closed everywhere, 449 tests. The strongest subsystem. |
| **Reconciliation** | 82 | Graded severity, critical blocks, system cannot self-resolve a capital discrepancy. Never run against real broker data. |
| **Security** | 78 | Zero credentials in source, no credential field at all in the IBKR config, mechanically re-checked. Low ceiling only because unpinned dependencies remain. |
| **Testing** | 80 | 2,982 tests, all ten critical categories covered, adversarial suites throughout. Distribution skews hard to recent phases. |
| **Observability** | 62 | Correlation ids, structured audit, health capabilities, alerts, journal — in the execution stack. The Phase 1–10 pipeline logs to stdout. |
| **API** | n/a | No HTTP API by design. The typed facade with `Caller` permissions is the right shape for what this is. |
| **Database** | 70 | Sound schema, good indexes where they matter, additive migrations, retention that keeps what research needs. FK enforcement off; 5 empty tables in production. |
| **Frontend** | 65 | Truthful — it reports absence as absence rather than rendering a confident zero. 3,853 lines with 15 tests. |
| **Deployment** | 58 | GitHub Actions + SQLite in a release asset works and is honest about its limits. 19 of 23 workflows are manual, which is the central operational problem. |

**Unweighted mean: 68.**

The number is less interesting than its shape: **safety scores high,
evidence scores low.** Execution 88, reconciliation 82, risk 80 — and
signals 35, models 50. This is a system built carefully to do something
it has not yet done.

## 2. Verdict

### READY FOR PAPER — and structurally incapable of anything more

| Level | Status | Reason |
|---|---|---|
| NOT READY | — | passed |
| PARTIALLY READY | — | passed |
| **READY FOR PAPER** | ✅ **current** | paper path works end to end against the mock; real risk engine consulted; durable sessions; reconciliation |
| READY FOR CONTROLLED LIVE | ❌ | blocked — see below |
| READY FOR PRODUCTION | ❌ | blocked |

### What blocks controlled live

Four things, in order of how hard they are to fix:

1. **No real-money execution path exists.** Not a configuration — an
   absence. Six independent enforcement points, and the sixth is that
   no adapter accepts a real-money environment. This is deliberate and
   correct, and it is a blocker by design.
2. **Never run against a real IBKR account.** Every test uses
   `MockIBKRTransport`. What a mock cannot prove is that IBKR behaves
   the way the mock does.
3. **No promotion gate can be measured.** Levels 4+ require 30 paper
   days and 30 paper trades. There have been zero trades.
4. **The signal layer emits nothing.** Five signals, fifteen
   suppressions, across five phases.

Note that (4) blocks (3) blocks (2). They are one problem wearing three
hats.

## 3. What is genuinely dangerous

Honestly, and the list is short:

- **Nothing that can lose money.** There is no real-money path, and six
  independent controls would each stop one if there were.
- **TD-04 — two order lifecycles.** Paper and broker paths diverge. A
  rule fixed in one will not appear in the other. This already happened
  once: the risk wiring existed only in the paper path until this phase.
- **Research validity is one module deep.** `PointInTimeView` is
  excellent and it is the only thing standing between this project and
  plausible-looking wrong numbers. 34 tests.
- **FK enforcement off.** Theoretical while the tables are empty. Fix
  before scheduling, not after.

## 4. What must happen before autonomous learning

Spec §76 names the next phase as trade-outcome intelligence. The
architecture supports it — all eight error classes in §52 are
representable, and `trade_outcomes` is indexed for exactly those
queries.

**It cannot begin, because there is no data.** A learning system needs
completed trades with outcomes. There are none, and there will be none
until signals stop being suppressed.

The prerequisite has now outlived six phases. Phase 12 named it, Phase
13 measured it, Phases 14–16 inherited it, Phase 17 confirms it:
**extending the price cache so the signal layer clears its own
confidence and sample-size floors is the first genuinely useful piece
of work available**, and it has never been an execution phase's to do.
